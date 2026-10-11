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
