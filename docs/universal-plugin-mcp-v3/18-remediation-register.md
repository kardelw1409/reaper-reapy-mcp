# 18 — Remediation Register (Phase C)

Status of every independent-audit finding (`AUD-001`…`AUD-015`) against the remediated documentation.
Permitted statuses: `OPEN` · `CORRECTED_IN_DOCS` · `BLOCKED_ON_OWNER` · `NOT_APPLICABLE_WITH_EVIDENCE`.

**Phase D (2026-10-10) did not change any status in this register**, and **Phase E does not either**:
Phase D consolidated the outstanding owner questions into four decisions
([19-owner-decisions.md](19-owner-decisions.md)); Phase E recorded the owner's approval of those four
and reconciled every finding against the implementation plan (§Phase E reconciliation below). The
owner-decision columns below point at the decisions instead of describing a request. Corpus-wide, the
distinction is kept explicitly in four states:

| State | Meaning | Where it lives |
|---|---|---|
| **1. Documentation corrected** | the false or overclaimed statement has been rewritten; `CORRECTED_IN_DOCS` | this register, "Status" column |
| **2. Implementation required** | the corrected behavior does not exist yet; it is built in the named implementation phase | "Implementation task" + [14](14-implementation-plan.md) |
| **3. Tests specified, not run** | the cases that will turn the specification into evidence; **none has been executed** | "Required tests" + [12](12-testing-strategy.md) |
| **4. Residual risk / limitation** | a property of the host or of V1 scope that implementation cannot remove | [15 §1](15-risks-and-open-questions.md) + [19 §7](19-owner-decisions.md) |

No finding moves to `VERIFIED` — documentation is not execution, and no test described here has been
run.

**No finding is marked `VERIFIED`.** The documentation has been corrected; the runtime behavior it
specifies has not been implemented or executed. "Corrected a false source claim" (done here) and
"the replacement implementation is proven" (in the implementation session, with tests) are different states and are kept
different on purpose.

Remediation workstreams (`DS-xx`) group the findings into coherent changes:

| DS | Workstream | Findings |
|---|---|---|
| DS-01 | Targeted state slice (P0 safety) | AUD-001 |
| DS-02 | Complete transactional recovery + honest evidence | AUD-002, AUD-015 |
| DS-03 | Identity model: profile fingerprint × target token × state revision | AUD-003 |
| DS-04 | Codec/Host transaction contract objects | AUD-004 |
| DS-05 | Legacy codec dependency isolation | AUD-005 |
| DS-06 | Lifecycle correctness + one package topology | AUD-006, AUD-013 |
| DS-07 | Normative outcome/error schema | AUD-007 |
| DS-08 | Cache provenance completeness + cache write safety | AUD-008, AUD-012 |
| DS-09 | Mutation policy consistency + V1 scope reduction | AUD-009 |
| DS-10 | Concurrency, cancellation, external changes | AUD-010 |
| DS-11 | Dependency-correct phase ordering | AUD-011 |
| DS-12 | Public-schema decisions and owner decision freeze | AUD-014 |

## Finding register

| ID | DS | Status | Affected documents | Corrected contract / source claim | Implementation task | Required tests | Owner decision | Evidence |
|---|---|---|---|---|---|---|---|---|
| AUD-001 | DS-01 | CORRECTED_IN_DOCS | 02, 03, 04, 08, 10, 11, 12, 17 | Removed the claim that the POC's multi-VST state targeting is safe; documented the first-block defect (`_b4_lib.find_vst_block`); defined `TargetedStateSlice` (span + header + framing + digest); bridge extracts/reinserts exactly the selected block; codec operates on the slice; mismatch ⇒ refusal before any mutation. No live state mutation before the mechanism + tests exist (P0/P3/P5 gates). | [14 P3/P5](14-implementation-plan.md) | [12 §5](12-testing-strategy.md) B-26…B-31 (multi-block fixtures, preceding foreign VST, two Vitals, FX movement, mismatch ⇒ 0 writes, untouched blocks byte-identical; plus mock criterion) | — | Verification [17 AV-01…AV-04, AV-10](17-planning-evidence-index.md); source `_b4_lib.py:108-111`; `bridge/state.py` read/write paths |
| AUD-002 | DS-02 | CORRECTED_IN_DOCS | 00, 04, 05, 06, 07, 11, 12, 14, 15 | "Verified rollback" is now scoped to **captured channels**; V1 requires capture + verified restore of **every mutable channel an operation can touch** (document, routes, full parameter vector) before the first write; document-restore resets parameters, so restoration order is state → parameters → verify state → verify parameters (provider-declared); incomplete capture ⇒ refuse before first write; outcomes distinguish `restored` / `partially_restored` / `unknown`; `FAILED_AND_RESTORED` may not be reported unless every declared channel verified. Q-06 promoted to V1 design blocker (resolved in docs; owner confirms). | [14 P1/P2/P5/P8](14-implementation-plan.md) | [12 §5](12-testing-strategy.md) B-32…B-37 (fail after state write ⇒ all channels restored; fail after parameter write; capture failure ⇒ 0 writes; truthful mismatch reporting; rollback failure ⇒ partial/unknown) | [19 Decision 1](19-owner-decisions.md) — confirm mandatory full-vector capture (cost measured in P5/P8) | Verification [17 AV-12, E-07](17-planning-evidence-index.md); `transaction.py:503-556`; `test_l4_live_write.py:101-141` |
| AUD-003 | DS-03 | CORRECTED_IN_DOCS | 05, 06, 07, 09, 12, 17 | Split into three concepts: **profile fingerprint** (cache compatibility; not instance identity), **write-target token** (session-scoped, opaque, server-side; revalidated before every write unit and parameter batch; stale/mismatched ⇒ refusal, never silent re-target; raw host pointers never serialized as durable identity), **state revision** (mutable; checked before sequential units and rollback; advances from verified read-back; self-conflicts excluded). Host-evidence limitation (identical-build replacement) documented; fail closed where indistinguishable; `TrackFX_GetFXGUID` behavior to be measured in P3. | [14 P1/P3](14-implementation-plan.md) | [12 §5](12-testing-strategy.md) B-38…B-43 (identical-build replacement; duplicate Vitals; FX movement; project change; stale revision; unexpected state change ⇒ no mutation) | Measure at P3 (FX-GUID behaviour); the residual limitation is accepted per [19 Decision 3](19-owner-decisions.md) | New contracts in [05 §5a](05-universal-core-contract.md); absence verified [17 AV-09](17-planning-evidence-index.md) |
| AUD-004 | DS-04 | CORRECTED_IN_DOCS | 04, 05, 08, 10, 11 | Defined the minimal explicit object set: `FxHostFacts`, `TargetedStateSlice`, `StateRevision`, `StateSnapshot`, `StateWriteUnit`, `StateWritePlan`, `WriteAttempt`, `VerificationReport` — each with purpose, minimum data, owner, invariants, tests. `structural_units()` replaced by `plan_state_writes()` (ordered units, not a count). `snapshot` is a Core-assembled transaction object (codec supplies document/routes parts). `HostBridge.write_state(..., verify)` removed — the bridge owns exact transport + targeted splice + settle/re-read; **no provider semantic callbacks in the bridge**; verification belongs to Core via the codec. | [14 P1/P5](14-implementation-plan.md) | [12 §4](12-testing-strategy.md) two mock codecs with different shapes/batching; sequential revisions; ordered writes; rebase; conflicts; rollback; verification; target preservation | — | [10 §1](10-state-modulation-dependencies.md); [08 §3-§4](08-provider-and-host-interfaces.md) |
| AUD-005 | DS-05 | CORRECTED_IN_DOCS | 02, 03, 08, 14, 15, 17 | Verified the import chain (`_b4_lib` → `_bh_session` → `reapy`, `os.chdir(...\src)`, `ReaperController`, `vital_*`). V1 forbids importing the legacy chain from Core or the provider; the codec-isolation task (P7) is resolved as approach (a): port the verified host-free function set ([17 AV-13](17-planning-evidence-index.md)) into the in-repo provider package's `codec.py`, with (b) a clean provider-owned codec from the verified algorithm + fixture corpus as the fallback ([19 Decision 2](19-owner-decisions.md)); trade-offs documented. Acceptance: provider import in a clean process must not import `reapy`/`ReaperController`/`_bh_session` nor change cwd; recursive import-boundary test. | [14 P7](14-implementation-plan.md) | [12 §2 L0/§4](12-testing-strategy.md) recursive import-boundary test; clean-process import check | Approve the isolation approach + placement — [19 Decision 2](19-owner-decisions.md) | Verification [17 AV-05, AV-13](17-planning-evidence-index.md); `_b4_lib.py:72-73,75-78,97-104,502-505`; `_bh_session.py:39-57,66-67` |
| AUD-006 | DS-06 | CORRECTED_IN_DOCS | 01, 06, 13, 14, 15 | Resolved the lifecycle contradiction with **Option A (recommended)**: the existing eager `reapy.connect()` at `BaseController.__init__` means the server process requires REAPER at start; the impossible "tool returns `NotConnected` when REAPER was never running" promise is removed. Documented: post-startup connection loss ⇒ typed `NotConnected` from the affected call (reapy raises; the bridge classifies — POC precedent). Option B (lazy startup) documented as out of V1 scope; requires explicit owner approval if wanted. | [14 P0/P9](14-implementation-plan.md) | [12 §5](12-testing-strategy.md) B-44 (post-startup loss ⇒ typed error); startup behavior documented, not "fixed" | [19 Decision 4](19-owner-decisions.md) — confirm Option A (Option B is a scope change, not a task) | [01 §3](01-existing-system-audit.md); `base_controller.py:31-33` |
| AUD-007 | DS-07 | CORRECTED_IN_DOCS | 05, 06, 11, 12, 13 | Envelope schema 2: `error` (typed) is separate from `outcome` (`stage`, `verdict`, `final_state` ∈ {`committed`,`unchanged_verified`,`partially_restored`,`unknown`}, `rollback{attempted, verified, channels}`, per-channel reports, host accounting `reads`/`write_calls_attempted`/`write_effects_verified`/`write_effects_unknown`). Failed post-mutation calls carry both `error` and the complete `outcome`. Zero-write claims allowed only when `write_calls_attempted == 0`. Failure taxonomy F1–F12 covers state write, parameter write, verification, rollback, connection loss, cancellation, revision conflict, preflight-after-reads. | [14 P1/P2](14-implementation-plan.md) | [12 §5](12-testing-strategy.md) B-45/B-46 + terminal-outcome matrix ([11 §3](11-planning-transactions-and-verification.md)) | — | [11 §3-§4](11-planning-transactions-and-verification.md); [06 §2](06-mcp-tool-contracts.md) |
| AUD-008 | DS-08 | CORRECTED_IN_DOCS | 07, 09, 12, 15, 17 | Cache key now covers every behaviour-affecting determinant: profile fingerprint + **provider revision/artifacts digest** (dependencies, laws, codec, write order, verification policy, limits) + source/registry digest + schema/format. Provider-declared `implementation_revision` + digest over declared artifacts; rule for incrementing documented; any behaviour-affecting change ⇒ new key ⇒ old profile never served. Missing required identity ⇒ fail closed: no writes, no modulation mutations, no restore, no validated-profile caching; limited diagnostic reads permitted but marked provisional. | [14 P4](14-implementation-plan.md) | [12 §5](12-testing-strategy.md) B-47 (key changes for dependency rule, codec revision, verification policy, provider version, registry data) | — | [09 §3](09-discovery-and-cache.md); [07 §1/§3/§5](07-capability-profile-schema.md) |
| AUD-009 | DS-09 | CORRECTED_IN_DOCS | 06, 10, 11, 12, 15 | One mutation policy: every mutator defaults to **not applying** (`dry_run=true`; explicit apply required) — parameter writes, state changes, modulation create/update/remove alike; tool-surface table classifies every operation read-only / dry-run / mutating / filesystem-mutating. Route mutations require explicit `slot` identity — the "first match" behaviour is allowed for reads/preview only; ambiguous mutation selectors refuse. Persistent restore points **deferred from V1** (Q-09 resolved-proposed); caller-controlled `settle_s` removed. | [14 P0/P9](14-implementation-plan.md) | [12 §5](12-testing-strategy.md) B-48/B-49 (ambiguous route mutation refused; dry-run default proven per tool) | Adopted defaults: dry-run default (Q-01 `RECOMMENDED_DEFAULT`), persistent restore points deferred (Q-09 `DEFERRED_FROM_V1`) — [19 §5](19-owner-decisions.md) | [06 §1/§7/§9](06-mcp-tool-contracts.md); [11 §6](11-planning-transactions-and-verification.md) |
| AUD-010 | DS-10 | CORRECTED_IN_DOCS | 04, 08, 11, 12, 15 | Concurrency contract: lock keyed to the **validated target token** (not a string/slot number); lock held from final resolution through the complete verified outcome/recovery; revision checks before each sequential write unit and before rollback, expected revision advanced by the transaction's own verified writes (no self-conflict); unexpected change ⇒ `StateConflict` before mutation / rollback on mid-sequence; cancellation = truthful final-state report (`unknown` where unknowable). **Explicit residual limitation:** no compare-and-swap exists in the host API, so revision comparison reduces but cannot eliminate external races; owner must accept this before guarantees freeze. | [14 P2/P3](14-implementation-plan.md) | [12 §5](12-testing-strategy.md) B-50…B-54 (concurrent mutation; cancellation during state/parameter write; stale revision; conflicting rollback; incomplete host outcome) | [19 Decision 3](19-owner-decisions.md) — accept the residual external-race limitation; rollback restores only channels whose revision still matches (`not_restored (external_change)` otherwise) | [11 §7](11-planning-transactions-and-verification.md); [04 I-15](04-architecture.md) |
| AUD-011 | DS-11 | CORRECTED_IN_DOCS | 12, 13, 14 | Phases rewritten to the dependency-correct sequence: **P0 contracts/decisions → P1 models+mocks → P2 transaction mechanics (mock) → P3 target-aware read-only bridge → P4 profile+cache → P5 host write primitives + codec contract → P6 mock modulation/dependency → P7 codec isolation + Vital provider → P8 Vital live mutation → P9 MCP integration → P10 E2E acceptance.** No live-write acceptance depends on later phases; tool registration is tested against mocks early (P2/P6) with production integration at P9. | [14](14-implementation-plan.md) | Per-phase acceptance in [14](14-implementation-plan.md); registration/mock tests at P2/P6 | — | [14 dependency graph](14-implementation-plan.md) |
| AUD-012 | DS-08 | CORRECTED_IN_DOCS | 02, 03, 09, 12, 17 | Removed the Phase-A overclaim that the POC preserves a foreign/mismatched file byte-identically. V3 cache rules: validate the existing file's identity before replacement; mismatched/invalid file ⇒ refuse-to-replace + **quarantine by atomic rename** (bytes preserved) + rebuild; write via temp file + `os.replace` (atomic where the FS guarantees it); interrupted writes leave only ignorable temp files; a partial target never appears. Foreign-preservation and atomicity are **new requirements**, verified by tests. | [14 P4](14-implementation-plan.md) | [12 §5](12-testing-strategy.md) B-55/B-56 (foreign file byte-identical after cache write; interrupted-write simulation) | — | [17 AV-06, AV-07](17-planning-evidence-index.md); `capability_cache.py:408-417,427-476` |
| AUD-013 | DS-06 | CORRECTED_IN_DOCS | 04, 08, 12, 13, 14, 15 | One package topology frozen: `src/reaper_reapy_mcp/plugins/` with `host/contract.py` + `host/reaper.py` (the only `reapy` importer **within `plugins/`**), core modules flat. The repo-wide "no reapy" claim is withdrawn — existing host modules already import reapy; the boundary test is scoped to `plugins/`. Registration frozen: **unconditional** registration of the 7 tools; no applicable provider ⇒ typed `UnsupportedProvider` result; registration never depends on provider-discovery side effects. | [14 P0/P9](14-implementation-plan.md) | [12 §2](12-testing-strategy.md) L0 scoped boundary tests; registration tests at P2 (mock) and P9 (server) | Adopted default: unconditional registration (Q-05 `RECOMMENDED_DEFAULT`) — [19 §5](19-owner-decisions.md) | [04 §6](04-architecture.md); [08 §8](08-provider-and-host-interfaces.md); [13 §3-§6](13-existing-mcp-integration.md) |
| AUD-014 | DS-12 | CORRECTED_IN_DOCS (owner questions resolved) | 06, 07, 14, 15, 17, 19 | All Q-01…Q-13 recorded with a status: the four escalated decisions were **`OWNER_APPROVED` on 2026-10-10** ([19-owner-decisions.md](19-owner-decisions.md)); the remainder are `RECOMMENDED_DEFAULT`, `DEFERRED_FROM_V1` or `RESOLVED_BY_SOURCE` ([15 §2](15-risks-and-open-questions.md), [19 §5](19-owner-decisions.md)): dry-run default; no legacy-codec import under any placement (in-repo package per [19 Decision 2](19-owner-decisions.md)); refuse unmeasured versions (no override); only V3-blocking defect fixes (recorded separately); unconditional registration; full-channel capture as the V1 blocker; user-local configurable cache; no raw escape hatch; defer persistent restore points; preserve the staged user change; VST2 fails closed; residual external race accepted as documented; eager startup retained. Every option exposed by the schema now has semantics, errors, tests, acceptance criteria. | [14 P0](14-implementation-plan.md) | Per-document tests listed in each question's row in [15 §2](15-risks-and-open-questions.md) | **Four decisions approved; everything else is an adopted default — no open owner dependency** | [15 §2](15-risks-and-open-questions.md); [19](19-owner-decisions.md) |
| AUD-015 | DS-02 | CORRECTED_IN_DOCS | 02, 12, 17 | L4 evidence claim corrected: the POC suite restores document+routes (route list predicate-verified) and does **not** restore the captured control vector. V3 L4 requires compare+restore of **every declared captured channel**, parameters included; "restored" is never claimed unless all manifest channels were compared after restore. | [14 P10](14-implementation-plan.md) | [12 §7](12-testing-strategy.md) L4 whole-channel restore assertions | — | [17 AV-08](17-planning-evidence-index.md); `test_l4_live_write.py:101-141` |

## Phase E reconciliation — what each finding needs next

Every finding is checked against the four states of the legend above. Nothing is "done" because it is
documented: each row names the phase that must build it, the cases that must demonstrate it, and any
limitation that survives implementation. Owner-decision pointers use the approved decisions
(2026-10-10, [19 §6](19-owner-decisions.md)).

| AUD | 1. Documentation corrected | 2. Implementation required (phase) | 3. Tests specified, not run | 4. Residual risk / limitation | Owner decision |
|---|---|---|---|---|---|
| AUD-001 | yes — first-block targeting defect and the `TargetedStateSlice` contract | P3 `locate_state_slice` (unique-or-refuse), P5 splice-only transport, P7 provider targeting | B-26…B-31; P8 multi-block byte-identity case | L-7 (splice fidelity unproven until a live multi-block write); R-21 | gate G-1/G-4 |
| AUD-002 | yes — "verified rollback" scoped to captured channels; full-vector capture required | P2 snapshot assembly + per-channel rollback, P5 codec restore, P8 live proofs | B-32…B-37, B-53; P8 harness cases | L-4 (capture cost unmeasured) | **Decision 1 approved** |
| AUD-003 | yes — fingerprint × target token × state revision separated | P1 identity objects, P3 token issuance/revalidation | B-38…B-43 | L-3 (identical-build replacement may be indistinguishable) | Decision 3 (locking/revalidation) |
| AUD-004 | yes — codec/bridge contract objects; no provider callbacks in the bridge | P5 codec contract + transport | Two-codec conformance suite | — | — |
| AUD-005 | yes — import chain verified; extraction boundary mapped (AV-13) | P7 port + V-12 build gate | V-12 clean-process import; byte-identity fixtures | R-03 (a ported function may still need the fallback path) | **Decision 2 approved** |
| AUD-006 | yes — lifecycle contradiction resolved; impossible promise removed | P0 documentation, P9 behaviour evidence | B-44; P9 registration/`NotConnected` rows | L-2 (no server ⇒ no invocation-level answer) | **Decision 4 approved** |
| AUD-007 | yes — envelope schema 2, `error` separate from `outcome`, counters | P2 outcome/counters, P9 tool layer | B-45, B-46; terminal-outcome matrix | R-20 (reporting drift under refactor pressure) | — |
| AUD-008 | yes — cache key completeness + fail-closed identity | P4 key composition and identity rules | B-47; P4 fail-closed row | R-06 (stale profile if the key regresses) | — |
| AUD-009 | yes — one mutation policy; slot identity; restore points deferred | P2 dry-run default, P6 slot/ambiguity rules | B-48, B-49 | — | — |
| AUD-010 | yes — concurrency contract + external-change rollback rule | P2 revision/lock mechanics, P3 revalidation, P8 live conflict behaviour | B-50…B-54; external-divergence rollback case | L-1 (no compare-and-swap exists) | **Decision 3 approved** |
| AUD-011 | yes — dependency-correct phase order P0–P10 | the phase order itself ([14](14-implementation-plan.md)) | per-phase test/exit criteria | — | gates G-8, G-10 |
| AUD-012 | yes — no byte-preserving overwrite claim; quarantine + atomic replace required | P4 cache write discipline | B-55, B-56 | — | — |
| AUD-013 | yes — one package topology; boundary tests scoped to `plugins/`; unconditional registration | P0 boundaries, P7 V-12, P9 registration | L0 boundary tests; V-12; P9-1 | — | — |
| AUD-014 | yes — all questions carry a status | — (no implementation; the questions are answered) | per-question cases in [12](12-testing-strategy.md) | deferred items remain deferred (Q-08, Q-09) | four decisions approved; the rest are adopted defaults ([19 §5](19-owner-decisions.md)) |
| AUD-015 | yes — POC L4 evidence corrected (control vector stored, never restored/compared) | P8 harness (compare every captured channel before "restored") | P8 cases; A-15 acceptance assertion | L-4 | **Decision 1 approved** |

**AUD-014 status change:** `BLOCKED_ON_OWNER` → `CORRECTED_IN_DOCS`. The owner approved the four
escalated decisions; every other question was a `RECOMMENDED_DEFAULT`/`DEFERRED_FROM_V1`/
`RESOLVED_BY_SOURCE` item that never required approval ([15 §2](15-risks-and-open-questions.md),
[19 §5](19-owner-decisions.md)). No item with a genuine, unresolved owner dependency remains.

**Workstreams (DS-01…DS-12) are unchanged** — all twelve remain in place and map onto the phases as
before: DS-01→P3/P5/P7, DS-02→P2/P5/P8, DS-03→P1/P3, DS-04→P5, DS-05→P7, DS-06→P0/P7/P9,
DS-07→P2/P9, DS-08→P4, DS-09→P2/P6, DS-10→P2/P3/P8, DS-11→the phase order, DS-12→the decision record.

## Summary

- **Critical (AUD-001..003):** corrected in docs; all three require runtime validation during the
  implementation session, with the gates G-1…G-6 holding before any live mutation is authorized.
- **High (AUD-004..011):** corrected in docs; each names its implementing phase and its tests.
- **Medium (AUD-012..015):** corrected in docs; every one now has a phase, a test set and, where it
  needed one, an owner decision that has been approved.
- **Owner decisions:** four, all `OWNER_APPROVED` (2026-10-10) — [19-owner-decisions.md](19-owner-decisions.md).
  No open owner dependency remains; the adopted defaults and deferrals are listed in [19 §5](19-owner-decisions.md).
- **Runtime validation pending everywhere.** Nothing in this register asserts that any behavior has
  been executed: every test row is a specification. The limitations that will survive implementation
  are listed in [19 §7](19-owner-decisions.md).

---

## Addendum — runtime validation status (2026-10-10, after block 3/3)

The sentence above described the register as it stood **before** the implementation session. For the
rows implemented in block 3/3 it is no longer accurate, and the difference is recorded here rather than
by editing the rows.

**Now executed against the live host** (evidence artifacts under `tests/plugins/live/evidence/`, phase
journal `IMPLEMENTATION-STATUS.md` §15–§17):

| Behaviour | Evidence |
| --- | --- |
| live identity, batched parameter read, slice location, bytes outside the span | `p8_baseline__*` (plus the read-conformance suite) |
| 775/775 live-verified control binding, 0 refused | `p8_baseline__20261010T195509Z__7eb5e9aa1a20.json` |
| document version gate inside the measured set | baseline artifact + `provider.document_version` tests |
| parameter write with independent read-back and verified restore | `p8_3_parameter_write__20261010T200037Z__8e1493d01ea9.json` |
| state-slice (modulation route) write with verified restore and intact outside-span bytes | `p8_4_state_write__20261010T202812Z__2580c9f0d484.json` |
| L-3 replacement measurement (handle does not change on repopulation; token does not refuse) | `p8_l3_identity__20261010T203101Z__dcd339e9c219.json` |
| end-to-end acceptance through the registered tools, dry run and committed write | `p10_acceptance__20261010T204137Z__c2fb13177e18.json` |
| registration: 7 tools through the real MCP client, defaults, zero-provider refusal | `tests/plugins/test_p9_tools.py` |

**Still specification-only or injected, and therefore not evidence of live behaviour:**

- `WriteNotApplied`, rollback ordering, host timeout and wedge classification — the live host produced
  the first two for real (once, during the derived-leaf incident, `p8_restore__20261010T201944Z__3e8d…`)
  and one wedge (before the held-session fix), but none can be induced on demand; the suites cover them
  by injection.
- Byte-exact restoration of the plugin-owned `settings.sample.samples` — deliberately not attempted;
  it is refused as a write target and reported as the named gap `derived_leaf_not_restored`.
- The live dependency-flag rules of P8.7 remain **declared but not evaluated** pending an owner
  decision ([IMPLEMENTATION-STATUS.md](../IMPLEMENTATION-STATUS.md) §20).

---

## Addendum 2 — release-review remediation (2026-10-10)

The owner reviewed the P8–P10 report and returned five decisions
([19 §8](19-owner-decisions.md)). What changed, and what was measured while changing it:

| Item | Status | Evidence / note |
| --- | --- | --- |
| `force` refused as `UnsupportedOverride` (no override in V1) | **implemented** | tool surface + Core; 22 policy tests in `tests/plugins/test_p11_force_and_identity.py` |
| Identity claims checked, continuity disclosed as `not_claimed` | **implemented** | `meta.identity_basis` on every response; caller-supplied identity/token mismatches refuse (`TargetChanged`) with 0 writes |
| Named gap `derived_leaf_not_restored` on every restore path + `rollback.complete` | **implemented** | `tests/plugins/test_p11_gaps.py` (internal outcome + the registered tool envelope) |
| Unevaluated rules: honest status + a guard on the one decidable subject | **implemented** | `tests/plugins/test_p11_rules.py`; the guard also runs against the real registry when present |
| `route_into_off_filter` / `wave_frame_needs_multiframe_wavetable` guards | **not enforceable from measured data** — *superseded for the first rule by Addendum 3* | the registry has no destination families; the second rule is informational by its own declaration → disclosed, never fired. Addendum 3 then made the **first** rule a refusal on the decidable subject (creating a pair); the second remains informational and has never fired |
| Structural DISABLE set writing display names as document paths | **defect found and fixed** | measured: 13 phantom keys created, no real setting changed (fixture), and the captured live document has 781 settings keys, none containing a space. Targets now resolve through the profile's measured bindings; unaddressable/unjustifiable targets are typed refusals (L-9) |
| "Every DISABLE candidate the plan keeps on" comparison | **defect found and fixed** | the comparison used `str(control_dict)`, so it never matched and every candidate was disabled regardless |
| Live write re-verification after this remediation | **deliberately not run** | the owner's brief forbids live mutations by default; the changes are covered by mocks, and the last live-write evidence (P8.3/P8.4/P10, 20:44–21:12) **predates** this remediation - recorded, not glossed |

---

## Addendum 3 — safety-gate correction (2026-10-11)

Owner decisions 6a/6b ([19 §9](19-owner-decisions.md)): two fail-closed contradictions were closed.

| Item | Status | Evidence / note |
| --- | --- | --- |
| `route_into_off_filter` disclosed but not gated while route changes were allowed | **corrected**: creating a pair is refused before the first write, for `dry_run` true and false, from `plugin_write` and `plugin_modulation` | `tests/plugins/test_p12_safety_gates.py`; guard derived from the codec's own route representation; **no invented destination mapping**, the rule is refused rather than faked |
| the gate's subject had to be read from every representation | **implemented**: `routes_create`, `routes_update` naming source/destination, state writes under the route table (including the table and a whole slot), and a world counting creates without specs | same file; `VC.make_routing`/`clear_routing` outputs are cross-checked against the guard so the two cannot drift |
| route creation availability with Vital | **blocked** (new limitation **L-10**) | the deciding inputs do not exist in the registry (destination families) or in the projected world (the filter flag) → recorded, not guessed |
| a token-carrying mutation accepted while continuity is unprovable | **corrected**: `IdentityContinuityUnprovable` before any host work, both surfaces, `dry_run` included | same file; reads keep accepting the token, location-addressed mutations stay available |
| `plugin_instances` workflow | **unchanged for addressing**: `data.instances[]` (location + identity) is the documented way to address a mutation; the token in `meta` is for reads/diagnostics | [05 §5](05-universal-core-contract.md), [06](06-mcp-tool-contracts.md) |
| live suite | **updated to the new policy, not run** | `test_p8_live_write.py` writes a document-only leaf now; `test_p10_acceptance.py` asserts the route refusal; both must be re-run under owner authorisation |
| new error code | `IdentityContinuityUnprovable` added to the closed taxonomy | [05 §7](05-universal-core-contract.md), `tests/plugins/test_p0_contracts.py` |

---

## Addendum 4 — live write/restore smoke test (2026-10-11, one write, owner-authorised)

One bounded live smoke test through the registered tools after the correction above.
Evidence: `tests/plugins/live/evidence/p12_live_smoke__20261011T041434Z__363904d8c23c.json`
(full record in `IMPLEMENTATION-STATUS.md` §23).

| Item | Status | Evidence / note |
| --- | --- | --- |
| one parameter write + restore through `plugin_write` | **confirmed by measurement**: dry run `NO_WRITES` with **0** write-verb calls at the bridge; the committed write `ALL_VERIFIED` with exactly **1** `write_params`; read-back delta **0.0**; restore `ALL_VERIFIED` with exactly **1** `write_params`; final **2858/2858 parameters exact**, **1071/1071 writable document leaves equal**, block digest equal, outside-span intact, no routes | the probe file `tests/plugins/live/test_p12_live_smoke.py`; the pytest run itself ended red on a **probe** defect (a report key the assertion did not have) *after* the restore had completed |
| a parameter write also moves a document leaf | **measured**: `n_mismatched: 1` of 1071 writable leaves after a parameter-only write, back at the baseline value by the end of the run | the leaf's path is not in the artifact (the probe summarised the diff and dropped the rows; fixed in the file, not re-measured) |
| the prescribed whole-document restore | **refused by the route gate** (new limitation **L-12**): the codec's change set is "every captured writable settings leaf", which contains the route table | `DependencyViolation` / `REFUSED_BEFORE_ANY_WRITE` / `write_calls_attempted: 0`; the gate cannot tell a restore from a create → owner decision needed (a recognisable restore intent, or a change set that avoids the table) |
| instance continuity | **still not provable** (**L-3**); the write was location-addressed and the revalidation is in-call only | `meta.identity_basis` on every response |

---

## Addendum 5 — P17/P18 practical acceptance (2026-10-11, live)

Three new tracks, three Vital instances, three basses, renders and the mandatory
freshness test — full record in `IMPLEMENTATION-STATUS.md` §24. Evidence:
`p17_three_instances__20261011T100201Z__e44f0be27515.json`,
`p17_three_instances__20261011T112833Z__9e68e0510239.json` (idempotent re-run),
`p18_bass_acceptance__20261011T112738Z__ce39b7583497.json`,
`p18_cache_freshness__20261011T112518Z__9c72a0fe6b03.json`, three `p18_bass_*.wav`.

| Item | Status | Evidence / note |
| --- | --- | --- |
| `add_fx` answered success for a **failed** add (`False >= 0`) | **fixed**: error envelope on failure; the added FX index is re-read from the track on success; `create_track` also returns its `track_index` | `tests/plugins/test_p17_host_tool_accounting.py` (4 tests, fake-controller, real FastMCP registration) |
| fuzzy `add_fx` names land the **VST2** build (`VSTi: Vital`), which the V3 provider does not match | **measured and handled**: the P17 probe tries the exact `VST3i: Vital (Vital Audio)` first and removes a landed non-matching FX before the next name; tracks verified to carry exactly one VST3i | `tests/plugins/live/test_p17_three_instances.py`; the accidental duplicates were removed via `remove_fx` and the chains verified |
| corpus-truncated ranges made portamento/slope/env-times/volume silently wrong | **fixed by live measurement** (`profile_build.LIVE_MEASURED_CORRECTIONS`; `values.py` power law gained an optional `offset`): portamento_time exponential `2**-10..2**4` s (a -3.3 request had landed at 0.900531 s), slope -8..+8, env attack/decay/release `32.0003 * norm ** 4`, volume `-80 + 86.02 * norm ** 0.5`; the registry files themselves are untouched | `tests/plugins/test_p18_live_corrections.py` (real-registry anchors); `tests/plugins/test_p16_measured_laws.py` now pins that only these rows differ from their declaration; transcript `tests/plugins/live/evidence/p18_calibration__20261011T114948Z__c91845f34b01.json` |
| dry-run plan did not expose its baseline | **fixed (additive)**: `plan.items[].from` is rendered; the freshness probe asserts it equals the externally-set value | `src/reaper_reapy_mcp/plugins/api.py`; `tests/plugins/live/test_p18_cache_freshness.py` |
| external change (classic `set_fx_param`) vs layer caches | **proved live**: V3 read 50.0 -> external 0.5-norm write -> V3 read 72.0 -> dry-run `from` 72.0 -> restore -> 50.0. No value cache or TTL exists to mask it | the freshness evidence above |
| `wave_frame` / spectral morph on the fresh instances | **measured inert** (single-frame Init table; two isolation renders with morph `Low Pass` @ 100 vs `None` produced identical metrics) — documented as inert, not as "supported"; both stay addressable | `IMPLEMENTATION-STATUS.md` §24.2 item 5 |
| three basses, one batch write each | **live**: 51/49/51 controls, `ALL_VERIFIED`, all rows agree; distinct measured characters (centroid 69.6 / 248.0 / 333.6 Hz; sub 0.956 / 0.781 / 0.707); 0 clipped samples; tuning error <= 0.06 st | acceptance evidence; WAVs beside it |
| batch-first accounting | **measured**: 13 workflow calls vs 453 naive single-control calls (x34.8); 3 host `write_params` calls for 151 controls | acceptance evidence `metrics` |

---

## Addendum 6 — P19: the 808 acceptance, and the enum-band defect it uncovered (2026-10-11, live)

One Vital instance (`V3-A Deep Glide 808`, track 1, fx 0) taken from "verified
writes that do not sound like an 808" to an accepted patch. Full record in
`IMPLEMENTATION-STATUS.md` §25. Evidence:
`p19_morph_audibility__20261011T131400Z__d7735b0e9fe9.json`,
`p19_osc_transpose_travel__20261011T133849Z__e14d5ad425b9.json`,
`p19_voice_transpose_space__20261011T134428Z__41f60fa0816b.json`,
`p19_808_shaping__20261011T140756Z__841d432401bd.json` (+ its WAVs),
`p19_808_shaping_reanalysis__20261011T141525Z__f3b6aa7dac07.json`,
`p19_enum_bands_through_host__20261011T143023Z__0f6794f11257.json`,
`p19_acceptance_808_patch__20261011T143231Z__65d79bbb3d3c.json` (+ the render),
`p19_morph_amounts__20261011T145943Z__085f13435aae.json`,
`p19_acceptance_detail__20261011T150230Z__55a4e96ba232.json`,
`p19_patch_host_verification__20261011T150343Z__2a4290fee6a6.json`.

An independent review of this round was run with **GLM-5.3 Flash** (the reviewer the
brief names) before reporting. It falsified one claim of mine (the morph), found a
real defect I had missed (the enum read leg), named the circularity of the layer's
verification, and constructed an estimator edge case - each item is in the table
below with what was measured afterwards.

| Item | Status | Evidence / note |
| --- | --- | --- |
| route creation refused wholesale, so no pitch drop could be expressed | **fixed**: `route_into_off_filter` is now an **evaluated** rule (destination->module join derived 316/316 from the registry, module switches derived, the decoded live document carried in the projected world); only a pair into an OFF Filter 1/2 is refused, an undecidable subject still fails closed with the missing input named | `tests/plugins/test_p19_route_rule.py`; `profile_build.state.structural_ops[0]` (`route_create`) is `available: true` and names its decider |
| a *declared* `enum_index` law is not the plugin's band map | **fixed**: `_declared_enum_positions` attaches measured band positions **only** where the uniform mapping puts a member outside its own measured band (3 controls: the filter styles, 4 of 5 members each) | live proof, same instance: `filter_1_style` "Notch Blend" @ 0.25 -> write "24dB" -> host reads **"24dB"** @ 0.125; `tests/plugins/test_p19_enum_positions.py` re-derives the corrected set from the registry sweeps |
| the layer's own read-back cannot falsify such a mapping | **measured, and this is the generalisable half**: write and verify ran the same wrong arithmetic, so `ALL_VERIFIED` was true and useless; the **host's formatter** (an independent channel) disagreed. Every enum control in the acceptance patch is now verified through REAPER as well | `p19_enum_bands_through_host__*.json`; §25.4 |
| an enum read back by *nearest midpoint* mis-resolves a value at a measured switch point | **fixed (found by the independent review)**: the measured band **switch points** are carried next to the midpoints and `values.normalized_to_native` resolves an enum by them; the host labels 0.0625 as "24dB" while midpoint distance called it the member below (5 of 64 grid points) | `tests/plugins/test_p19_enum_positions.py::test_the_read_leg_resolves_every_measured_grid_point_of_the_host` (all 65 host-labelled points, plus the counter-example) |
| "the spectral morph is inert" | **corrected - the claim was too strong**, and the review falsified it from the round's own WAVs: `@25`/`@60` change the waveform (0.0595 / 0.0062 max diff) while the *metrics* stay identical at every amount (h2 -22.22, centroid 55.4 Hz, sub 0.9994). The measured facts are now: amount 100 is a no-op on this table, the render does respond to other amounts, and none of them is what separates a saw from a sub through this filter - the filter is | `p19_morph_amounts__20261011T145943Z__085f13435aae.json`; §25.1 item 3 |
| the acceptance's numeric verification is law-derived on both legs (circular) | **answered with an independent read-only check**: the plugin's own formatted text for 11 numeric controls matches the intent in its own unit (`filter_1_cutoff` `-26.0805 semitones` = 58.0 Hz, `env_2_*` in secs, `filter_1_resonance` `4%`, ...) | `p19_patch_host_verification__20261011T150343Z__2a4290fee6a6.json` |
| the pitch-drop *depth* is not verifiable by a windowed estimator | **stated as a bound, not as a measurement**: a true 12 st glide reads at most +7.5 st through the 2048-sample instrument; measured facts are the drop's existence at every note, its monotonicity in the requested depth, and a 0.235 s settle | `p19_acceptance_detail__20261011T150230Z__55a4e96ba232.json`; §25.5 |
| the pitch estimator resolves an octave high on an H2-dominant signal | **accepted limitation, pinned as a test** (constructed by the review); cannot arise from these materials (sub h2 >= 16 dB down, saw h2 6.8 dB down) | `tests/plugins/test_p19_pitch_estimator.py` |
| the filter cutoffs were written in the wrong value space | **pinned, not changed**: the profile's space is the plugin's document value (`display = document - 60`, `Hz = 261.625565 * 2 ** ((document - 60) / 12)`); the previous recipe's 50/86/96 were 147/1183/2093 Hz | `tests/plugins/test_p19_filter_units.py`; the acceptance patch's cutoff (document 33.92) reads **-26.0805 semitones -> 58.0 Hz** through the host |
| the spectral morph the previous recipe relied on | **measured inert** on the single-frame "Init" table (Low Pass @ 100 bit-identical to None @ 0) - documented as inert, never as a mechanism | `p19_morph_audibility__*.json` |
| `voice_transpose`'s corpus range made the documented neutral play +10 st sharp | **fixed by live measurement** (22nd corrected row: the caller's value *is* the displayed semitone offset, -48..+48) | `tests/plugins/test_p18_live_corrections.py`; probe H |
| the codec's route `power` default came from a fixture | **fixed**: default `0.0`, recorded against the corpus (16868/17036 routes) and the live empty slot | `providers/vital/codec.py`; `provider.py` revision r6 |
| the **probe harness** mis-measured low fundamentals (biased autocorrelation: 41.2 Hz read as 400.9 Hz) | **fixed and pinned**: bias-corrected estimator + shortest-period rule + a stated resolution limit (no 2048-sample frame below 100 Hz) + a separate short-window instrument for a fast drop (with its own bound: a window average is a lower bound) | `tests/plugins/test_p19_pitch_estimator.py` (11 tests); the P18 baseline re-reads -0.01/-0.02/-0.02 st, so no earlier conclusion depended on it |
| `fm_needs_modulator` on the parameter channel; `plugin_modulation update` dropping `power`; probe B2's unexplained `WriteNotApplied` | **open, named** - the acceptance patch did not depend on any of them (empty route table, power on create, both channels verified) | §25.5 |

## Addendum 7 — P20: the reference-guided 808, and the compressor nobody had set (2026-10-11, live)

Same instance (`V3-A Deep Glide 808`, track 1, fx 0) taken from "closed sub with a
click" to a patch fitted to a concrete audio reference
(`Stickz FBT - 808 10 - F#.wav`) within **1.46 dB RMS** over h1..h7. Full record in
`IMPLEMENTATION-STATUS.md` §26. Evidence: `p20_reference_analysis__20261011T152613Z__8c8da617c9bf.json`
(+ plots, + audition WAVs), `p20_baseline_read__*`, `p20_probe_a_onset_source__*`,
`p20_probe_b_compressor_filter__*`, `p20_probe_c_saturation__*`,
`p20_probe_d_candidate__*`, `p20_probe_e_cutoff_sweep__*`, `p20_probe_f_combined__*`,
`p20_probe_g_attack_options__*`, `p20_reanalysis__20261011T164622Z__188dd175360e.json`,
`p20_final_808_patch__20261011T170029Z__288d943358cf.json` (+ the render and the
loudness-aligned A/B copies), `p20_final_plot_*`.

An independent review (GLM-5.3 Flash, read-only) re-measured the reference with its
own instruments and **corrected one of my inferences** (the drop settles in ~45-60 ms,
not 26 ms), reproduced the compressor finding from the arm WAVs, showed the FX
`Distortion Type` enum is *not* affected by the P19 band defect (uniform
`index/(n-1)` lands every member in its own measured band), and pointed at the
corpus' own published version of the recipe.

| Item | Status | Evidence / note |
| --- | --- | --- |
| **the click** in the failed patch | **found and removed**: the **multiband compressor FX was ON** (`mix 0.85`, low/high band +16.3 dB, band +11.7 dB) - never part of any P19 patch, it survived from the P18 bass recipe. Only `compressor_on = Off` takes the onset step from **13.6 % of peak (21x the body's slope)** to **0.55 % (0.9x)**; the arms that keep it reproduce the click bit-near-identically | probes A (`p20_probe_a_a3...wav`) and B (`p20_probe_b_b1...wav`); §26.2 |
| **the closed sub** | **measured as missing harmonics, not a closed filter**: cutoff 58/116/232 Hz give h2 −20.2/−8.7/−7.8; saturating the *filtered* sub post-filter (Soft Clip +24 dB, **mix 0.20**, cutoff **50 Hz**) fits the reference's series to **1.46 dB RMS** | probes B/C/D/E + `p20_reanalysis__*`; the recipe is also `bass_synthesis_techniques.md` §8.6 (corpus, found by the review) |
| the attack is *dragged* by the lowpass (no zero crossing for 18-21 ms) | **fixed by measurement**: a second route `env_2 -> filter_1_cutoff` (+12 st) opens the cutoff while the pitch is high; first half-cycle 8.7 ms, onset step 2.78 % of peak = **1.79x the body slope**, softer than the reference's own 3.3x | probe G (`g2_sweep12`); §26.3 |
| `filter_1_keytrack = 100 %` on a bass patch | **trap measured and recorded**: the cutoff follows the *key*, so an E1 note closes the filter (h1 −47 dB, the sub dies). Keytrack is not a pitch-tracking mechanism | probe G (`g1_keytrack`) |
| `routes_update` with a semantic `depth` (documented in 06 §8/§9) | **fixed (defect D1)**: only `routes_create` converted a depth, so the update arrived at the codec with a field it does not own and was refused as *"an update names no field to change"*. The provider now converts it; an update naming only a depth is refused with `detail.reason = "depth_without_destination"`; amount+depth keeps the amount. **The final patch writes its 18 st depth semantically** | `tests/plugins/test_p20_routes_update_depth.py` (8 tests); contract §8 updated |
| the arm tables were measured with a body window that contained the **next note** | **instrument defect found and fixed**: `[onset+0.30, onset+0.80]` overlaps the phrase's second note (starts 0.7 s in) and lifted h2 by up to 4.6 dB; default is now `[onset+0.25, onset+0.55]` and **every arm was re-derived from the same WAVs** | `p20_reanalysis__20261011T164622Z__188dd175360e.json` |
| a ±3 % harmonic band can contain **no FFT bin** on a short window | **instrument defect found and fixed**: the band is never narrower than 1.5 bins, and a fundamental with no bin refuses with the resolution named instead of returning zeros | `probe_p20_sample.py` (`_band_mask`); §26.4 |
| "the filter's slope is ~11 dB/oct, so the `24dB` style may be inert" | **refuted by the corrected removal of the compressor confound**: the slope is ~20 dB/oct and the `12dB` arm measures h2 −13.4 vs −20.2 at the same cutoff - the style is effective | probes B/C (`c6_style_12db`); §26.2 |
| the reference's drop settles in ~26 ms | **corrected by the independent review**: ~45-60 ms (my 26 ms came from a 5-frame rule over a 46 ms instrument, i.e. two cycles); the cycle-based start (+18.2 st) stands | review §1(b); §26.1 |
| the drop's **depth** in the final patch | **still a bound**: the same instrument reads the reference at 7.68 st and the final render at 9.91 st; both average a <60 ms flight. Cycle-based facts: the reference +18.2 st, the arms monotone in the requested depth | §26.7 |
| h3 is 3 dB strong in the final fit (−16.5 vs −19.6) | **accepted**: it is the largest single deviation of a ≤1.5 dB fit; the neighbours (drive, mix) trade h5/h7 for it | §26.7 |
| loudness: the reference is a normalised sample (0 dBFS, −7.1 RMS) | **stated, not matched**: the patch is left at −3 dB (peak −7.8 dBFS); the A/B copies in the evidence are loudness-aligned instead | `p20_ab_final_*` |
| `fm_needs_modulator` on the parameter channel; `power` dropped by `plugin_modulation update`; probe-B2 `WriteNotApplied` | **still open, unchanged by this round** | §25.5, §26.7 |

## Addendum 8 — P21: capability cache, warm path, preload, startup (2026-10-11)

Full record in `IMPLEMENTATION-STATUS.md` §27. The round asked whether plugin
knowledge is *prepared, cached, loaded at startup and reused* - or re-researched
on every request. Four defects were found by reading the code and measured:

1. **the warm path projected the whole profile before consulting the cache**
   (and threw it away on a hit): 583-630 ms of pure compute per warm call on the
   real registry. The P4 hot-path test could not see it - it counted *host
   parameter reads*, and a build performs none;
2. **nothing was loaded at startup** - the first call after every server start
   paid the full cold path even with a prepared profile on disk;
3. **a provider object was built per call**, so the 6.6 MB registry was re-read,
   re-parsed and re-hashed every time (124-176 ms);
4. **the instance sweep read every slot of every track** although the caller's
   selector named the instance (measured 1536 ms for 4 slots).

| Item | Status | Evidence / note |
| --- | --- | --- |
| warm call rebuilds the profile it discards | **fixed**: the provider declares its key material without a build (`profile_source_rows`, hash-only registry rows, measured 8.6 ms against 155 ms); a miss cross-checks that the built provenance re-derives the declared key, else `cache.key_material` refuses the write | `resolve.py`, `cache.py::key_material_of`, `providers/vital/provider.py`; `test_p21_warm_path_and_preload.py` (16 tests) |
| nothing loaded at startup | **fixed**: `plugins/preload.py` validates every identity-independent property of the cache entries once at `create_server`, holds them in memory, never writes/quarantines/deletes, never raises; a moved file or disagreeing identity falls through to the full disk path | `preload.py`, `server.py`; evidence `p21_cache_timing__20261011T185938Z__e2e67032fa1e.json`; 10 preload tests |
| the preload scan was unbounded on a large cache | **fixed after the independent review**: superseded entries dismissed by a text prefilter (no parse), per-provider cap of 8 newest, extra files reported as `preload.capped` and never deleted | `preload.py::PRELOAD_ENTRY_CAP_PER_PROVIDER/_newest_first`; `test_the_preload_holds_at_most_the_cap_and_reports_the_rest` |
| the sweep read every slot | **fixed**: a selector naming `track_index` reads only that track, naming `fx_index` only that slot; measured 1536 ms -> 687 ms | `resolve.py::iter_instances`; `test_a_location_selector_reads_only_the_addressed_tracks_slots` |
| the served profile was not handed to the provider | **fixed**: `adopt_profile` hook, called on every serve path (rules/targets read the provider's own state) | `cache.py::adopt_profile`, `resolve.py`, `providers/vital/provider.py` |
| 2858 host parameters vs 775 mapped controls | **explained, every parameter classified**: `2858 = 775 mapped + 2080 MIDI-CC slots + 3 REAPER wrapper params + 0 unclassified`; the MIDI-CC block and the wrapper names were **read live** (`MIDI CC N\|M`, `Bypass`/`Wet`/`Delta`) | `tests/plugins/live/probe_p21_profile_completeness.py`; evidence `p21_profile_completeness__20261011T185928Z__2f5f2fea60b5.json`; pinned by `test_p21_profile_completeness.py` |
| profile incompleteness had no honest inventory | **reported**: 6 controls with no law, 2 unmeasured, 163 without a range, 590 without a unit, 36 weak-evidence (the `env_*_power`/`env_*_delay` families), 51 of 342 enums carrying measured (non-uniform) band positions | the completeness artifact; §27.4 |
| `registry_source_rows` was claimed "pinned by a test" with no such test | **fixed** (found by the independent review): the hash-only rows, `load_registry`'s rows and the built provenance are now pinned equal - on the fixture registry and on the measured one | `test_registry_source_rows_equal_the_rows_load_registry_records`, `test_registry_source_rows_equal_on_the_measured_registry_too` |
| stale entries accumulate without policy | **reported, not deleted**: the preload names every superseded entry (`preload.superseded`) and the bytes it occupies; deletion stays manual (09 §4 policy) | evidence `p21_cache_timing__*` `real_cache_preload` (12-13 entries, 0 parses) |
| the instance sweep is now the whole warm call (687 ms), its RPCs unheld | **open, named**: wrapping `list_tracks`/`fx_facts` in reapy's held context is a transport change with its own measurement duty | §27.10 |
| `plugin_write`'s bridge-verb count (23; `fx_facts` x8; `param_idents` x6 with 5 full-table reads) | **quoted from the independent study, not re-measured** | §27.8, §27.10 |

## Addendum 9 — P22: automatic capability discovery from a read-only host channel (2026-10-11)

Full record in `IMPLEMENTATION-STATUS.md` §28. The round asked how much of the
profile's **incompleteness** can be closed automatically at *preparation* time,
without installing an unproven value and without paying for research on every MCP
start. The channel is one read-only host call,
`TrackFX_FormatParamValueNormalized` - it *formats* a normalized value the caller
passes, so it cannot move a parameter - plus `TrackFX_GetParameterStepSizes`.
Everything the round learned about the host was measured live and is recorded in
§28.2, including two **measured absences**: `TrackFX_GetParamEx` reports the
uniform 0..1 space for all 775 controls (no semantic range), and
`TrackFX_GetNamedConfigParm 'param.N.default_value'` answers 0 for all 775 (no
defaults).

| Item | Status | Evidence / note |
| --- | --- | --- |
| 8 controls whose sweeps fitted no law | **4 closed**: `direction: descending` is now first-class in the value model (spine = `1 - normalized`, range stays numeric extremes) for `affine`/`power`/`exponential` - `delay_frequency`, `phaser_frequency`, `delay_aux_frequency`; `delay_aux_tempo` was a **label wearing a number** (`"4/1"`, `"1/2"`) and is now an enum | `values.py::direction/to_normalized/normalized_to_native`; `profile_build.derive_measured_law` |
| the fit's acceptance bound was a global constant, so a five-decade display was refused although every state sat on the curve | **fixed**: the bound is `max(per-state printed precision, half the smallest measured step)`. Verified **strictly additive** - 3 new laws, no previously accepted law changed | `profile_build._display_precision/_measurement_entries`; `test_p16_measured_laws.py` |
| 36 `weak` rows | **24 promoted to `strong`** by an *independent instrument* (the host formatter) agreeing at every grid point inside each state's printed precision (622 already-strong rows also confirmed; 24 confirmed rows had `strength_before: weak`, recorded per control) | evidence `p22_host_calibration__20261011T212110Z__49536b6cfc9c.json`; §28.4 |
| enums whose boundaries were arithmetic (291 of 342) | **count unchanged, correctness changed**: every measured map now carries `switch_points` (band *starts*, so an exact boundary reads the member that starts there) and the arithmetic path rounds **half-up** - measured against the host: normalized 0.5 formats `"On"`, 0.25 on an 11-member enum formats `"8/1"`. Half-to-even read both as the wrong member | `profile_build._band_geometry`; `values.py::normalized_to_native`; `test_a_value_exactly_on_an_enum_boundary_reads_the_member_that_starts_there` |
| 590 controls with no unit | **0 filled, and that is the finding**: the host prints no unit for them; every control (185) whose text carries a stable unit token already declared it. `units_filled: 0` with the absence recorded, never invented | §28.2, §28.4 |
| 163 controls with no range | **162** - and the gain came from the value model (`delay_aux_tempo`), not from the artifact: a range the host does not state is not guessed | §28.1 |
| near-fits had no honest container | **candidates**: 4 `law_candidate` rows (`random_1..4_frequency`, worst ratio 11.2) and 19 `display_offset_candidate` rows (among them the P19 `-60` semitone family). A candidate has **zero consumers**; validation requires a candidate's worst ratio > 1 **and** a law that cannot convert | `profile_build.derive_law_candidate`; `profile.py` rows 6c |
| the artifact itself (does a new "prepared data" file create a new trust problem?) | **bounded by construction**: the merge takes **no bridge**, imports no host module (pinned by a test), and can only *annotate*. The artifact's bytes are a **declared source**, so its hash is cache-key material - a changed, removed **or refused** artifact all move the key | `providers/vital/calibration.py`; `test_p22_calibration.py` key-material tests |
| does the MCP start pay for the research? | **no**: `create_server` runs only the preload - 13 files, **13 skipped, 0 parses, 0.0415 s**; startup 1.4823 s + 0.1239 s unchanged. Cold/warm/warm-index call: 1947.8 / 763.8 / 674.7 ms | evidence `p21_cache_timing__20261011T212208Z__c7a3c8b9feb3.json` |
| **review finding (WEAK): the unit fill trusted the artifact's own `unit_token`** - samples `"dB"` with `unit_token: "Hz"` filled `unit = "Hz"` with a "measured" provenance note | **fixed**: the token and the `numeric` flag are re-derived **from the samples** inside the merge; a disagreement fills nothing and is recorded as `claim_disagrees_with_samples` - and now **travels into the profile** (`calibration.units_declined`), where before it was dropped | `calibration.py::_unit_token_from_samples`; `test_a_unit_claim_that_contradicts_its_own_samples_is_declined`, `test_a_numeric_claim_over_label_samples_fills_nothing` |
| **review finding (DEFECT): two untyped crash paths** - non-numeric `steps.step` raised a raw `ValueError`; a crafted sample `"1e999999"` overflowed `0.5*10.0**n` inside the merge | **fixed**: typed `IncompatibleSettings` for non-numeric/non-finite/oversized step values and grid points outside 0..1 or out of order; a guarded precision reader treats an impossible magnitude as *unreadable* (never a huge tolerance); non-finite samples are skipped instead of compared as `inf`; the validator now rejects the `NaN`/`Infinity` tokens `json` parses. Sample/artifact/grid size bounds and a scalar-only projection of the artifact's proof blocks were added in the same pass | `calibration.py::load_calibration/display_precision/_confirmation_record`; `profile.py::_is_number`; `profile_build._scalar_subset/_MAX_DECIMAL_EXPONENT`; 6 regression tests |
| re-verification of those fixes | the **reviewer's own** probe scripts were re-run against the fixed code (offline): `B: applied OK`, `C1/C2: unit None`, `D: refused IncompatibleSettings`, `E: host_steps_valid` flagged; key-material probe unchanged (`fast rows == build sources: True`) | §28.8 |
| 1,059 of the 1,088 named gap instances (29 closed: 24 promotions, 4 new laws, 1 law-shape change) | **open, with reasons** (§28.7): 590 units and 162 ranges the host does not state, 291 arithmetic enum maps whose registry sweeps are uniform, 4 ratio-parametrized controls needing a law kind this model lacks, 12 two-point `weak` sweeps | §28.7, §28.10 |
| applying the 19 offset candidates | **deliberately not done**: an offset applied to the profile's own mapping is a mapping change with its own before/after duty, and the round's brief forbade installing unproven values | §28.7, §28.10 |
| the artifact's `generated_utc` moves on every re-run, so a re-capture invalidates the cache entry even when the measurements are identical | **accepted**: a source whose bytes are key material cannot be exempted from moving the key | §28.10 |
| `host_steps` recorded verbatim; a `law_candidate`'s `refused_by` string unvalidated | **accepted, named**: the host's step metadata is an instrument report this layer cannot independently reproduce; nothing consumes a candidate | §28.10 |

## Addendum 10 — P23: adaptive read-only enum boundaries, and a proven v2 preload (2026-10-12)

Full record in `IMPLEMENTATION-STATUS.md` §29. Two questions: (A) can more enum band
boundaries be discovered through **adaptive read-only formatting** (only
`TrackFX_FormatParamValueNormalized`, which formats a value the caller passes)? (B) does
the startup preload actually load a **compatible** schema-v2 profile and let the next
warm call use it without a rebuild or a disk read? P22 could only show entries
*rejected*, which does not prove the positive case.

| Item | Status | Evidence / note |
| --- | --- | --- |
| the sweep-derived boundaries were upper bounds, not boundaries | **explained, then measured past**: the sweep samples a grid (1/256, or 1/16 for the styles), so a change point is an upper bound at the grid's resolution; 5,320 of 5,671 boundaries sat up to 3.35e-3 from the uniform arithmetic *inside* that resolution | census `probe_p23_enum_gap_census.py`; §29.1 |
| adaptive read-only discovery | **implemented**: anchors → two-call candidate proof → bracket + bisection down to adjacent doubles → third-label classification → band-interior check → unlabelled-tail measurement; 343 controls, 5,952 transitions, 36,895 calls, 37.9 s, read-only proof True/True | `probe_p23_enum_boundaries.py`; artifact 5,180,158 bytes; §29.2 |
| the evidence levels | **kept apart**: `exact_at` 815 (the candidate double flips the display and the double below it does not), `exact` 16 (bracket = adjacent doubles), `continuous_display` 5,120, `unresolved` 1; band interiors 1,153/1,153 verified | §29.2 |
| cross-run reproducibility | **measured**: 5,951 of 5,951 boundaries byte-identical between a run from a boundary-free profile and a run from a profile that already carried them (the second proves each in two calls and reports `exact_at`) | §29.2 |
| the merge (what may change) | **fail-closed**: attach measured `switch_points` (full precision) + evidence only where the measurement contradicts the profile by more than the bracket's own width (322 controls); refuse a control whose transitions are not all measurable, whose `confirmed_within` bracket does not contain the profile's boundary, whose interiors were not verified, or whose write target cannot sit inside its own band; name (never invent) the ramp controls | `enum_boundaries.py`; §29.3 |
| acceptance against the host | **833 → 0** read disagreements with the host's own printout, tested at the live value, both ends of every measured bracket and every write target (8,297 comparisons); write targets 6,293/6,293 in both runs | `probe_p23_read_agreement.py`; §29.4 |
| the ramp controls | **characterized, not converted**: 20 controls whose "257 members" are the sweep's sampling grid of a linear display (`distortion_filter_blend` slope ≈ 2.0); geometry untouched, `state: no_band_structure` recorded; the correct fix (a numeric law) changes write acceptance and is a provider-data decision | §29.7 |
| `bypass` | **unresolved and named**: the host prints `0`/`60` for the two-state wrapper parameter the registry calls `normal`/`bypassed` | §29.7 |
| a compatible v2 profile at startup | **proved positively** through the real `create_server()`: 1 file discovered, 1 valid, **1 parsed and loaded**, 0 rejected, preload 0.0272 s; warm lookup `source=cache`, `prepared=True`, **builds 0**, **disk reads 0**, content hash equal | `test_p23_preload_positive.py`, `probe_p23_preload_offline.py`; §29.5 |
| negative cases must not stop the valid entry | **asserted**: a schema-v1 entry and a stale v2 entry beside it ⇒ 3 discovered, 1 loaded, 2 rejected (`preload.superseded` 1, `preload.format_key` 1); nothing deleted, quarantined or rewritten by starting up; with nothing valid the call builds once (existing fallback) | §29.5 |
| values are still read live | **asserted through the tool layer**: after the host's value changes, `plugin_read` returns the new value and `read_params` is called again | §29.5 |
| review A3: a write target could escape its own band (midpoint rounded to 9 decimals; two-ULP band) | **fixed**: exact, clamped midpoint; the merge asserts `band_ends[i] <= position < band_ends[i+1]` for every attached target (closed at the top of the domain unless a tail caps it) and refuses otherwise; the bands travel in the profile and the validator re-checks containment | `enum_boundaries.py`, `profile.py`; §29.6 |
| review A4: `confirmed_within` accepted without being about the profile's boundary | **fixed**: the merge requires `lo <= boundary <= hi` for that state and refuses otherwise; the level's meaning is documented in the profile's `evidence_levels` | §29.6 |
| review A6: the recorded artifact digest was not the digest of the file (LF→CRLF translation on write) | **fixed**: both probes write with `newline="\n"`, verify the digest by re-reading the file, and both artifacts were regenerated (P22's content unchanged) | `probe_p22_host_calibration.py`, `probe_p23_enum_boundaries.py`; §29.6 |
| review Q1(b)/F3: brackets say nothing about band interiors (spikes, coarse printed digits) | **fixed**: the probe verifies every band's interior; the merge refuses unverified ones. The new check found a bug in my own probe (the tail was measured after the interiors) which is fixed and re-run to 1,153/1,153 | §29.6 |
| review Q3: one vacuous assertion | **fixed**: replaced by a counting subclass | §29.6 |
| warm path and startup cost | **unchanged in shape**: cold 2139.1 ms / warm-on-disk 782.1 ms / warm-in-index 682.8 ms (builds 1/0/0); import 1.5154 s + `create_server` 0.129 s; 14 cache entries preloaded in 0.2106 s with 0 parses | `p23_…` evidence, `p21_cache_timing__20261011T235802Z__d67e5fc0f5ff.json` |
| profile size | **+18%**: 1,347,048 → 1,590,914 bytes (measured bands + evidence + `band_ends`, which is what lets containment be re-checked offline) | §29.3, §29.10 |
| the 20 ramp controls and `bypass` | **open, with reasons** (§29.7): a ramp has no bands to measure, and the wrapper's member vocabulary is not what the host prints | §29.7 |
| boundaries are exact in the host's **display** space only | **stated**: a plugin-internal representation below the formatter is not observable through this channel | §29.10 |

## Addendum 11 — P24: the twenty declared enums that are ramps, and a second provider used only by tests (2026-10-12)

Full record in `IMPLEMENTATION-STATUS.md` §30. Two questions: (A) fix the remaining
wrong or under-proven Vital control model — the 20 controls P23 had *characterized* as
ramps while the profile still served them as 257-member enums; (B) does the shared
Capability Profile / cache / preload layer really carry a **second** plugin through its
own provider, or does it only work because there has only ever been one? Task B was to
be answered with a **test-only** provider: no real provider added, nothing declared
supported.

| Item | Status | Evidence / note |
| --- | --- | --- |
| the 20 declared `enum_index` rows whose display changed at all 256 swept steps | **converted to `number` with a measured law** (all `affine`); profile 432/343 → **452 numeric / 323 enum**; every one of the 775 controls now carries a converting law (`law_candidate` 0) | `profile_build.py` (`_sampled_ramp`), `test_p24_continuous_controls.py`; §30.2-30.3 |
| the criterion, so a banded member set cannot be swallowed | **requires all of**: 257 sampled positions (`steps: 256`), every display a faithful number, strictly monotone, all distinct, plus one fitting numeric law. `pitch_bend_range` (49 states) and `oversampling` (4) keep their members, bands and `switch_points` | §30.3 |
| the instrument model the fit depends on | **measured, not assumed**: float32 printed with `%.6g`; positions stored to 6 decimals (worth ~11× the printed precision at a steep end); budget = printed precision + half a float32 ulp; fit judged at `i/(n-1)`, not at the stored value | §30.2 |
| the four `random_N_frequency` rows P23 refused | **now derive** as `exponential` (16 octaves of period): the refusal was the instrument's, not the data's | §30.4 |
| live acceptance (read-only) | ramps **1400/1400** within one printed digit (worst 0.523), conversion round trips **240/240**; ratios 448/448 numeric reads, 484/490 within two units; the pre-P24 model answered **43/1400** of the same points; `writes_attempted: 0`, watched values and chunk sha unchanged | `probe_p24_continuous_agreement.py`, evidence `p24_continuous_agreement__20261012T082013Z__80cb83cd1c4e.json`; §30.5 |
| the three unnamed `filter_N_style` states | **sourced, not invented**: `mtytel/vital` `kFilterStyleNames` (5 names) vs `synth_parameters.cpp` (`style` over 0.0..9.0) vs the model-dependent lookup in `filter_section.cpp`; `unlabelled_regions` carries `why` + `source` | `enum_boundaries.py`, `test_p24_unlabelled_states.py`; §30.6 |
| a **second** provider through the real shared paths | **implemented as a test-only provider** and exercised through `key_material_of`, cache save/load, the preload index, `plugin_read`/`plugin_write` and `create_server()`: own key `da63ef27…`, warm call `built: false`, coexistence without collisions, typed refusal when a plugin has no provider, stale/superseded entries rejected without collateral loss | `tests/plugins/second_provider.py`, `test_p24_second_provider.py`; §30.7 |
| **cross-provider profile leak** (found by that test) | **fixed**: `ProfileCache(provider_id="second").load(<vital key>)` served the *other* provider's profile as a hit. `_read_entry` refuses a foreign `provider_id` (typed refusal, **no quarantine**, the entry survives and its owner still loads it) regardless of whether an identity was passed; `save()` refuses a foreign profile up front | `cache.py`; §30.7 |
| the profile change had to reach a warm installation | **revision moved** `vital-provider-2026.10-r10` → `r11`; measured live in a temporary cache dir: cold `source=built` with the entry's provenance at `r11`, warm `source=cache, built=false`, r10 and r11 keys differ (`fe5f10c9…` vs `9709316d…`) and the r10 file is untouched | `provider.py`, `probe_p24_warm_revision.py`; §30.9 |
| review **M1**: the new validator row promised "a real range" and did not check one | **fixed**: a converted control must carry a usable range (`min < max`, finite), raised as `refuse_rebuild` — the kind the loader honours (only `rebuild_required`/`cacheable` are consulted, never `ok`), so a self-consistent corrupt entry is quarantined instead of served | `profile.py`; §30.8 |
| review **M2**: `artifact_confirmation` was attached to a record that measured nothing | **fixed**: the confirmation requires *every* transition to be `continuous_display`; otherwise it is withheld and named (`confirmations_withheld`, 0 in the served profile — all 5,120 ramp transitions are `continuous_display`) | `enum_boundaries.py`, `profile_build.py`; §30.8 |
| review **m1/m2**: a vacuous digest assertion, and a fixture restore that was only byte-preserving by accident (line-end translation, untracked file) | **fixed**: the assertion compares against the previous material's digest; the fixture is rewritten and restored with `write_bytes` plus a byte-equality assertion (both fixtures verified unchanged) | `test_p24_second_provider.py`; §30.8 |
| review **m4**: the range census counted a correct **descending** law as a disagreement | **fixed**: compared in each control's own orientation; re-run gives **452 checked / 349 agree / 103 disagree** (the 7 descending rows were the artefact) | `probe_p24_continuous_agreement.py`; §30.8, §30.10 |
| review **m3**: two of the 20 are integer ladders (`effect_chain_order`, `sample_transpose_quantize`) modelled as continuous affine laws | **named gap, not papered over**: the display never printed a fraction at 70 arbitrary points each; what the host *stores* for a fractional request needs a marked live write, which this round forbids. `sampled_display.printed_values_are_integers` describes the sweep's samples, not the display's resolution | §30.8, §30.11 |
| review **n1-n4** | `_exact_sampled_positions` now requires ascending stored positions; the r11 rationale corrected (the provider's own source files are already key material); `if True:` in `cache.py` is pre-existing at HEAD and left alone; `controls_confirmed: 0` is pre-existing P23 behaviour (`positions_moved` 0) | §30.8 |
| **103 declared ranges disagree with the live display endpoints** (pre-existing, now measured with the orientation fixed) | **open, with the fix named**: `filter_1_cutoff` declared 8..136 vs −52..140 displayed; the P22 calibration artifact already carries `first`/`last`/`samples`/`unit_token` per control, so the next round can recalibrate the 103 rows | §30.10, §30.11 |
| `bypass`, the `%`-space trap | **unchanged and named**: `bypass` prints `0`/`60` for members `normal`/`bypassed` (1 unresolved transition, refused by name); percent controls stay an evidence row with no conversion | §30.11 |

## Addendum 12 — P25: range integrity, the two integer ladders, and the unnamed states (2026-10-12)

Full record in `IMPLEMENTATION-STATUS.md` §31. Tasks: (A) check and fix the 103 declared
ranges that disagreed with the live display; (B) decide the model of the two controls
that never display a fraction; (C) settle `bypass` and `filter_N_style` semantics if the
evidence supports it. The round's rule: eliminate *proven* metadata errors and separate a
confirmed range from a displayed one from a real semantic from an unknown - not fill
fields. No live write; no new provider; the cache/preload mechanism untouched.

| Item | Status | Evidence / note |
| --- | --- | --- |
| the 103 "disagreements" | **split by measurement**: **51** rows were the *same range in two spaces* (the census compared the state range against the display transform: `value - 60` semitones, `value * 100` %, `2**value` s, `value**4`); **52** were genuinely narrower than the parameter | `p25_range_audit` reconstruction; §31.1 |
| the instrument | **the plugin's own parameter table** (`mtytel/vital` `synth_parameters.cpp` + `value_bridge.h` + `synth_strings.h`), **checked against the loaded build**: 7,650 samples over 450 controls - 7,565 identical texts, 85 equal numbers with a unit-label difference (5 named controls), **0 numbers differ**, 3 controls the public branch does not declare | `source_facts.py` (`CORRESPONDENCE`); §31.2 |
| the correction | **70 ranges widened to the plugin's own bounds**, all of them in the state space: of the 452 numeric controls, 225 are state-space rows (**155 equal / 70 strict subsets / 0 wider / 0 shifted**); the other 227 are sweep-fitted or live-measured and live in the display space, so the source's bounds say nothing about them | `profile_build.py`, `source_facts.SOURCE_BOUNDS`; §31.3 |
| the rule | **widen only** (`widen_to_source_bounds` returns `None` for equal/wider/shifted/absent), applied only in the *declared* branch, with `evidence.value_bounds` (before/after/widened/why/source/registry-fit corroboration/`evidence_level`/`value_space.display` incl. the live text at both ends) and a profile-level `value_bounds` block | `profile_build.py`; §31.3 |
| why it mattered (measured) | `lfo_1_frequency`'s own minimum mapped to normalized **-0.34** (unreachable); `eq_high_cutoff`'s declared minimum 31.2 **refused the plugin's own 8**; `sample_pan`'s cap 0.06 truncated the pan space; `modulation_NN_power`'s ranges were a few presets' usage against the source's -10..+10 | §31.3 |
| task B: the two ladders | **numeric with proven discretisation**: both are `ValueDetails::kIndexed` (span 362879 = 9!-1, and 8191); `isDiscrete()` is false (span >= 300) so the host reports **no step**, while `convertToEngineValue` rounds every host-set value - a fractional request is stored rounded. `law.params.quantization = {step 1, origin 0, round half away from zero}` + `discrete: true`; `values` snaps both directions onto the grid (clamped to the grid within the range) | `source_facts.INTEGER_LADDERS`, `values._snap_to_grid`; §31.4 |
| the round trip is no longer the identity, by construction | normalized 0.5 on `effect_chain_order` is state 181439.5, the engine holds **181440**, the read-back is 0.5000014 - the tests allow half a step instead of pretending the ladder is continuous | `test_p25_value_bounds.py`, `test_p16_measured_laws.py`; §31.4 |
| task C: `bypass` | **explained**: its own row carries `display_multiply = 60.0` and `string_lookup = nullptr`, so the host prints `0`/`60` and there is **no state label to measure** - the transition is `unresolved` *by construction*, and `normal`/`bypassed` are recorded as the registry's words, not the plugin's | `enum_boundaries.py`, `source_facts.NO_LABEL_TABLE`; §31.5 |
| task C: `filter_N_style` | **no contextual dependency needed**: `getText` uses the five-entry `kFilterStyleNames` unconditionally, so the *parameter's* text does not depend on `filter_N_model` (the model-dependent naming is the editor's, recorded per model, not modelled); the release's span is **8** (its own discriminating probe: at 0.058/0.06 the host prints "12dB" where span 9 predicts "24dB") while the public branch declares 0..9 - both recorded; the unnamed band (indices 5..8, from 0.5625) prints **""** live | `enum_boundaries.py`, `source_facts.FILTER_STYLE_FACTS`; §31.5 |
| the `%`-unit trap (a defect the range work exposed) | **narrowed to percent spaces**: `1.0` is refused only where the range proves percent (bounds beyond 1, e.g. `filter_1_keytrack` -100..100, unchanged) or where no range can prove either space (unchanged); on the **27** fraction-space controls the refusal was wrong - `1.0` is their top, and live it formats as the host's **"100%"** | `values.unit_trap`; §31.6 |
| invalidation | **revision r11 -> r12** and `source_facts.py` added to `_PROVIDER_FILES` (key material); a stale r11 entry is neither served nor touched | `provider.py`, test in `test_p25_value_bounds.py`; §31.7 |
| live acceptance (read-only) | bounds **70/70** host text = recorded live text (69/70 vs the source text, the 1 the named `voice_tune` unit drift); fraction tops **6/6** -> "100%"; ladder write conversions **7/7** and **6/6** read back exactly; `bypass` `0,0,0,60,60,60,60`; the styles' five bands 5/5 and `""` for the unnamed one; `writes_attempted: 0`, watched values and chunk sha unchanged | `probe_p25_range_acceptance.py`, evidence `p25_range_acceptance__20261012T110127Z__7ba5826e08c7.json` (`110032Z`/`110104Z` retained, superseded); §31.9 |
| review **F1** (MEDIUM-LOW): two fail-open branches in the new validator row | **reproduced then fixed**: a tampered `range` validated `ok` and loaded as a **cache hit**; now both branches are `refuse_rebuild`, the range must equal the record's `declared_after`, and the loader quarantines it (`hit` -> `refused/profile_invalid`) | `profile.py`; §31.8 |
| review **F2** (LOW): the cited evidence's span discriminator was vacuous | **accepted**: the authoritative run is `110127Z__7ba5826e08c7.json`; the two earlier runs are named as superseded | §31.8 |
| review **F3/F4/F5** | stale count fixed (51); a declared grid the range does not sit on is now refused (`refuse_rebuild`) so the latent snap-vs-engine divergence cannot be declared; the "integer" claim is scoped to the host write channel in the facts, comments and tests | `profile.py`, `source_facts.py`; §31.8 |
| tests | **672 passed** offline (+33 new; the updated pins: `artifact_paths()` 8 -> 9, the two ladders discrete *with* a grid, half-a-step round-trip tolerance for a quantized control, a declared range may differ from the declaration only by the fact-table widening) + **20** repo regressions | §31.10 |
| residual gaps (named) | the 227 display-space rows are not narrowed against the source; the ladder write experiment is **not run** (prediction recorded); an engine-set value was not probed; release-vs-public deltas beyond the five unit strings and three missing parameters are bounded only by the 450-control sample; `sampled_display.printed_values_are_integers` still misnames the sweep's samples; one unreproduced `test_p4_cache` b55 one-off | §31.11 |
