# 12 — Testing Strategy

Status: **[D] proposed** (rev. 2026-10-11, Phase C remediation → Phase D consolidation — see [18](18-remediation-register.md) and [19](19-owner-decisions.md)).
Test-first obligations for Phase E, derived from the POC's proven layering
[A: 02 §16] and its recorded defects [A: 02 §17], adapted to the target repo's tooling, **extended
by the Phase-C audit findings** (targeting, complete recovery, identity, codec contract, isolation,
outcome schema, cache safety — AUD-001…AUD-015).

## 1. Principles

1. **Offline by default.** The whole unit/contract/mock suite must run with no REAPER installed.
   Live tests are separately selected and never part of the default run.
2. **Evidence, not existence.** A test file's existence proves nothing; each test names the contract
   or invariant it verifies, and results are recorded (counts + failures) per run.
3. **Failure paths are the product.** The suite must make every verdict in
   [11 §3](11-planning-transactions-and-verification.md) reachable; a test suite that only exercises
   successful writes does not test this system.
4. **No wall-clock sleeps in tests.** Settle/retry timings are injected parameters.
5. **Recorded orgs are pinned.** Where a test asserts against recorded evidence (e.g. the POC's
   depth-refusal set), the test pins the *artifact* and its content hash, and the artifact names the
   run that produced it (the POC's two-writer rule: stable snapshots vs per-run evidence [A]).
6. **The POC suite stays the Vital oracle.** V3 does not re-run or modify POC tests during Phase E
   without owner instruction (they write evidence artifacts when run — **[A]** flagged in [02 §16]);
   regression comparisons use *recorded* POC outputs, not re-executions.

## 2. Layers

| Layer | Runs where | Content | Gate |
|---|---|---|---|
| **L0 boundary/static** | offline | import-boundary tests scoped to `plugins/` ([08 §8](08-provider-and-host-interfaces.md)); no `reapy` outside `plugins/host/reaper.py` **within `plugins/`**; no provider identifiers in core; **recursive provider-import test** (AUD-005: provider import in a clean process pulls in no `reapy`/`ReaperController`/`_bh_session`/`_b4_lib` and does not chdir); tool-name collision check against the 57 existing names; **mutation-default audit**: every mutator's registration spec declares `dry_run` default `true` (AUD-009); doc/envelope schema snapshots | every commit |
| **L1 unit (pure)** | offline | domain model validation; value laws incl. `none`; enum discreteness; `%`-trap; profile schema checks; cache keying; finding evaluation; plan building; transaction state machine on mocks; envelope shapes | every commit |
| **L2 contract** | offline | provider-conformance suite run on the **mock provider**; codec-conformance suite; host-bridge conformance run on a **mock host** (the REAPER bridge runs only its read-only subset when live); tool contract tests via FastMCP in-process client | every commit |
| **L3 live read-only** | REAPER running | resolve/identify; `plugin_instances/describe/find/read`; state inspect; cache behaviour (miss/build/hit/rebuild); identity-change handling | manual/selected |
| **L4 live write (self-restoring)** | REAPER running | `plugin_write`/`plugin_state` restore/`plugin_modulation` mutations against a designated **test project + test instance**; every case snapshots before and restores after; refuses to run against an unresponsive plugin | explicit opt-in only |
| **L5 Vital regression** | offline against recorded artifacts + L3/L4 live | POC-measured invariants: ident addressing, doc-before-params order, one-route-per-write, depth acceptance/refusal set, dependency refusals, cache refusals | phase exits |

Layer naming intentionally matches the POC's L1/L3/L4 convention so evidence can be compared 1:1
[A: 02 §16].

## 3. Unit tests (L1) — required contents

- **Models:** every field table in [05 §2] / [07 §1] round-trips; required-field absence refused.
- **Values/laws:** each `law.kind` converts and inverse-converts within tolerance; `none`/`unmeasured`
  refuse conversion with reason; unit traps refused; discrete enums never interpolate.
- **Profile validation:** [07 §4] table — one test per row.
- **Cache:** [09 §9] — key matrix; five refusals; truncation fails content-hash **and** count checks;
  foreign key byte-identical; no-identity ⇒ no file.
- **Findings:** each `kind`; `hard` ⇒ refusal object; informational hard (+`informational`) does not
  refuse; projected-world fixture (plan that is correct only after projection passes).
- **Plan:** items name channels; unknown names produce suggestions; unrooted state path refused;
  disable-set (structural transition) produced when the provider declares one.
- **Transaction (mock host+provider):** stage order; document-before-parameters call order asserted
  on the mock's call log; reversed order refused; dry-run (the default) performs zero writes; every
  verdict (8) reachable with its `final_state` mapping; rollback verified **per captured channel**;
  `ROLLBACK_FAILED` loud; `FAILED_AND_RESTORED` withheld while any channel is unverified;
  `write_calls_attempted == 0` on refusals; snapshot-or-refuse (capture manifest complete or the
  mutation refuses); verify compares in the written space (regression fixture reproducing the POC's
  three-false-mismatch defect); "API says success, stores nothing" mock ⇒ `WriteNotApplied`;
  token/revision revalidation before each unit; counters consistent (attempted/verified/unknown).
- **Envelope:** ok/fail/dry shapes stable (schema 2); typed error codes; `error` and `outcome`
  coexisting on failed post-mutation calls; `meta` carries instance + target token + profile key.

## 4. Contract tests (L2) — suites and what they pin

| Suite | Questions it pins |
|---|---|
| **Provider conformance** | provider resolves an identity; builds a schema-valid profile; profile round-trips; `normalize_request` refusals are typed; `write_order` is a valid permutation; `verification_policy` yields comparators for every declared value type; `convert_depth` refuses with reasons where declared |
| **Codec conformance** | decode(readback) after encode reproduces changes; unrooted paths refused; `structural_units()` respected by the engine; snapshot/restore equality on a synthetic document |
| **Host-bridge conformance** | read params batch returns in order; ident round-trip; state read/write is opaque pass-through; timeout produces `Wedged`; transport death produces `NotConnected`; no swallowing anywhere; **counters**: one host call per batch, not per control |
| **Profile/schema conformance** | [07] validation suite (shared with L1) |
| **MCP tool contract** | each of the 7 tools: schema snapshot; required-arg validation produces the envelope refusal (not a Python traceback); unknown tool arg rejected; registers alongside the 57 without collision |

## 5. Boundary tests (explicit list — brief §19.3 + source-derived)

| # | Case | Expected |
|---|---|---|
| B-1 | unsupported capability (state on a parameter-only provider fixture) | typed refusal |
| B-2 | missing instance | `InstanceNotFound` |
| B-3 | stale instance (fingerprint changed between resolve and write) | re-resolve once → `StaleInstance` or proceed on the new single candidate; never silent |
| B-4 | ambiguous instance without selector | `AmbiguousInstance` |
| B-5 | unknown control/module/route | refusal + suggestions |
| B-6 | invalid type / out-of-range / unknown enum | refusal with legal values/range |
| B-7 | read-only control written | refusal naming capability |
| B-8 | state write to a control whose authority is parameter | refusal (and inverse) |
| B-9 | unmet dependency (hard) | refusal, `write_calls_attempted == 0` |
| B-10 | plugin version ∉ compatible set | `VersionMismatch`; **writes refused, no override** ([07 §3.2](07-capability-profile-schema.md), Q-03) |
| B-11 | corrupt/invalidated cache | quarantine + rebuild + warning; never serve |
| B-12 | state decode failure | `Unreadable` |
| B-13 | write failure (host raises) | typed transport/`WriteNotApplied`, rollback attempted |
| B-14 | read-back mismatch on state | `WriteNotApplied`, rollback verified per channel |
| B-15 | read-back mismatch on parameter | per-item row, rollback |
| B-16 | successful rollback on every captured channel | `FAILED_AND_RESTORED` / `unchanged_verified`, per-channel evidence |
| B-17 | rollback unverified/failed | `ROLLBACK_FAILED` (or `FAILED_AND_NOT_RESTORED`), loud, `final_state: unknown` |
| B-18 | host disconnected mid-operation | `NotConnected`; `write_effects_unknown` where a write was issued |
| B-19 | plugin instance removed during operation | typed failure, no crash, no wrong-instance write |
| B-20 | wedge (host call never returns) | `Wedged` after timeout; `write_effects_unknown > 0`; `final_state: unknown` — never "unchanged" |
| B-21 | unrooted state path | refusal |
| B-22 | value-space mismatch regression | no false mismatch (fixture from [11 §2.5](11-planning-transactions-and-verification.md)) |
| B-23 | multi-unit structural write, unit 3 fails | rollback of units 1-2 verified per captured channel |
| B-24 | cache concurrency (two writers) | no partial file visible |
| B-25 | tool-name collision with existing 57 | test fails at registration |
| **B-26** | mock chunk with multiple VST blocks, target = a non-first block | only the intended block changes; read/decode/verify address it; zero writes on ambiguity |
| **B-27** | preceding non-Vital VST block before the Vital block | targeting by class/ordinal still selects Vital; the foreign block is never decoded as a document |
| **B-28** | two Vital instances on one track | reads/writes target the addressed instance; the other instance's block is byte-identical after the write |
| **B-29** | adjacent VST2/VST3 plugin blocks (where the fixture supports it) | module-string discrimination works; wrong-block write impossible |
| **B-30** | FX-slot movement (FX indices shift between resolve and write) | token revalidation detects the move; refusal (`TargetChanged`); zero writes; no silent re-target |
| **B-31** | ambiguous/mismatched block correlation (class count vs facts disagree) | `TargetSliceAmbiguous`; zero writes; untouched blocks byte-identical |
| **B-32** | capture incomplete (one channel unreadable at snapshot) | `SnapshotUnavailable`; zero writes |
| **B-33** | failure after the state write, before parameters | rollback restores document **and** captured parameters; both channels verified |
| **B-34** | failure after the parameter write | rollback restores every captured channel; `final_state` reflects actual verification |
| **B-35** | parameter read-back mismatch during rollback | reported truthfully (`partially_restored`/`unknown`), never `FAILED_AND_RESTORED` |
| **B-36** | rollback failure (restore raises) | `ROLLBACK_FAILED`; loud; `final_state: unknown` |
| **B-37** | document-reset fixture: document restore would reset parameters | restore order (document → parameters) proven on the mock call log; parameters verified after |
| **B-38** | identical-build replacement in the same slot (fixture: facts indistinguishable) | fail closed per the P3 measurement outcome; never a silent write to the replacement |
| **B-39** | duplicated Vital instances, selector omitted | `AmbiguousInstance`; no write |
| **B-40** | project change during the session | stale context detected; refusal, no write |
| **B-41** | target token stale (facts differ from token) | `TargetChanged`; zero writes |
| **B-42** | unexpected external state change between plan and unit | `StateConflict`; zero writes (or rollback mid-sequence) |
| **B-43** | revision advance after the transaction's own verified write | no self-conflict on multi-unit sequences |
| **B-44** | post-startup connection loss | typed `NotConnected` from the affected call (AUD-006 Option A: no claim for a server that never started) |
| **B-45** | terminal-outcome matrix: every verdict × final_state combination | exactly the [11 §3](11-planning-transactions-and-verification.md) mapping; no unreachable/ambiguous state |
| **B-46** | counters: refusal (`write_calls_attempted==0`), failed commit (writes ≥ 1, effects_verified + effects_unknown consistent), attempted-but-unverified rollback | counters consistent in every outcome |
| **B-47** | cache key change matrix: dependency rules, codec revision, verification policy, provider version, registry data | each ⇒ new key; old profile never served (AUD-008) |
| **B-48** | ambiguous route selector on a mutation op | typed refusal; first-match permitted for list/preview only (AUD-009) |
| **B-49** | mutation-default audit: every mutating tool/op | applies nothing unless `dry_run:false` explicitly passed (AUD-009) |
| **B-50** | concurrent mutation attempts on one target | serialized by the target-token lock; second operation observes the first's verified outcome or conflicts, never interleaves |
| **B-51** | cancellation during a state write | truthful final-state report (`write_effects_unknown` or verified), rollback attempted (AUD-010) |
| **B-52** | cancellation during a parameter write | same; no assumption that stopping prevented mutation |
| **B-53** | stale expected revision at rollback time | reported in the outcome; `partially_restored`/`unknown`, never hidden; a channel with externally divergent state is left **untouched** and reported `not_restored (external_change)` ([19 Decision 3](19-owner-decisions.md)) |
| **B-54** | incomplete host outcome (transport dies after write verb returns) | `write_effects_unknown > 0`; `final_state: unknown`; never "unchanged" |
| **B-55** | foreign/mismatched cache file at the expected path | quarantined by atomic rename; original bytes byte-identical at the quarantine path; fresh file written atomically (AUD-012) |
| **B-56** | interrupted cache write (kill between temp and replace) | no partial target ever readable; stray temp ignored; next load behaves as "no file" or old file |

## 6. Mock strategy (offline determinism)

- **`MockHostBridge`**: in-memory tracks/FX/params/state; records a call log (order, counts);
  failure injection per call (`raise`, `wedge`, `store-nothing`, `drop-after-return`); parameter
  store keyed by ident; **multi-block chunk fixtures** (foreign VST, two Vitals, moved slots) with
  byte-comparison of untouched blocks; a **replaceable instance handle** so identical-build
  replacement can be simulated (AUD-001/AUD-003).
  This is the instrument for L1/L2: the transaction, cache, plan, dependency engine are all tested
  against it — **no REAPER needed** (contrast: the target repo's current tests mostly require live
  REAPER [A: 01 §6]).
- **`MockProvider` + a second mock codec with a different document shape** (envelope-wrapped
  key/value tree, different batching: two units for one logical change) — the codec-conformance
  suite runs on both (AUD-004).
- **`MockProvider`**: tiny parameter-only plugin with one state-only control, one enum with a
  `%`-trap, a two-route model, and a declared hard dependency — exercises the full matrix without
  any Vital knowledge. It doubles as the in-repo example provider.
- **Token/revision fixtures**: the mock issues real target tokens and computes real revisions so
  B-38…B-43, B-50…B-54 run offline.
- **Recorded-fixture mode:** provider data-derived tests (Vital) read pinned JSON fixtures copied
  *from recorded artifacts*, never from a re-run.
- **Deterministic values:** no randomness in values; timing injected; JSON outputs sorted-key for
  byte comparison (POC `results.dumps` precedent [A]).

## 7. Live REAPER integration tests (L3/L4)

Prerequisites (documented per test file):

1. REAPER running, reapy server enabled (`reaper_side_enable_server.py`) [A: 01 §3].
2. A **designated test project** with a known plugin instance (Vital at a known track/FX), never the
   user's working project; the test refuses to run if the resolved instance's identity mismatches the
   expected test instance.
3. Plugin responsive (a cheap document read succeeds); on failure → reconnect attempt loop, then
   **exit without writing** (POC L4 precedent: "Refusing to run a write suite against an unresponsive
   plugin", exit code 2 [A]).

Safety rules for L4:

- Every case: session capture → mutate → verify → restore → verify restore; the restore result is
  part of the test output. **The capture and the restore cover every declared captured channel —
  document, routes and the parameter vector — and every channel is compared after restoration
  (AUD-015). "Restored" is never printed unless all manifest-captured channels were compared.**
  (The POC's L4 precedent restores document+routes only — its stored control vector is unused
  [A: 02 §16, 17 AV-08]; V3 explicitly does better.)
- No test may alter any control the test does not name **and does not capture** (verified by
  before/after digests of the whole parameter space where feasible — the POC's "watch the whole
  range, refuse a short read" lesson [A]).
- State writes in L4 must use the production `TargetedStateSlice` path — no other block may change;
  a case on a multi-block test track proves the untouched blocks are byte-identical.
- Artifacts: per-run JSON under a logs directory with the run timestamp + content hash; never
  overwrite a previous run's file (POC two-writer rule [A]).
- Nondeterminism policy: any test whose verdict depends on timing/render declares its tolerance and
  records it; "reproducible across runs" is measured (out-of-process), not asserted from one run [A:
  POC reproducibility lesson].

## 8. Vital regression obligations (L5)

The Vital provider must reproduce, with recorded comparisons:

| # | Behavior | Evidence anchor |
|---|---|---|
| V-1 | parameters written by VST3 ident, never index; index refresh inside the hop | POC `parameters.write_by_ident` |
| V-2 | document-before-parameters order; a document write resets parameters (the reason) | POC measurement + transaction probe |
| V-3 | one route per state write; N routes ⇒ N sequential verified writes | POC `phase3_route_count/sequential_routes` [C] |
| V-4 | depth conversion acceptance/refusal set matches the recorded fit (246/316; named refusals) | POC `vital_scales_full.json` fit outputs [C] |
| V-5 | dependency refusals: FM-without-modulator, route-to-off-filter, `%`-trap, DIRECT OUT informational | POC `dependencies.py` + tests |
| V-6 | verification tolerances: state 1e-5, parameter 1e-3; restore 1e-6 both-channel | POC transaction/executor |
| V-7 | **capture/restore manifest: document + routes + full parameter vector; document-restore-then-parameter-restore order; every channel verified after restore** (V3 contract, superseding the POC's document-only capture — AUD-002/015) | POC DEC-19 (gap) + [11 §2.3a](11-planning-transactions-and-verification.md) |
| V-8 | cache refusal cases + quarantine/atomic-write behaviour (V3 contract, superseding the POC's plain overwrite — AUD-012) | POC `capability_cache.py` + [09 §5](09-discovery-and-cache.md) |
| V-9 | `line_mapping` write refusal + strip warning | POC DEC-25 |
| V-10 | version gate: advisory at describe; **hard refusal at write, no override** ([07 §3.2](07-capability-profile-schema.md), AUD-014 Q-03) | POC `locate`/`assert_version` |
| V-11 | **slice-exact state targeting**: on a multi-block track, the Vital block changes and no other block's bytes change (V3 contract, correcting the POC's first-block path — AUD-001) | [02 §10](02-vital-poc-audit.md) + [17 AV-01…AV-04](17-planning-evidence-index.md) |
| V-12 | **provider isolation**: importing the provider in a clean process imports no `reapy`/`ReaperController`/`_bh_session`/`_b4_lib` and does not chdir (AUD-005 acceptance) | [17 AV-05](17-planning-evidence-index.md) |

## 9. Acceptance criteria template (per implementation phase)

Every phase in [14](14-implementation-plan.md) exits only when:

1. Its listed tests exist and pass **offline** (unless the phase's tests are L3/L4 by definition —
   then the recorded run result is attached as evidence).
2. The phase's boundary property is asserted by a test (e.g. "zero write calls on refusal",
   "untouched blocks byte-identical", "provider import pulls no legacy modules").
3. No previously passing test regressed; the failure count is reported, not implied.
4. Any recorded evidence artifacts are named with the run identity and never overwrite prior runs.
5. For phases that touch live state: the phase's evidence shows every captured channel restored and
   compared, or the outcome says exactly which channel was not — never an implied success.
6. The exit conditions in [14] are met, including "documented in the phase's PR/notes".

## 10. Failure conditions (what makes a phase fail regardless of test count)

- A test that asserts success where the host stored nothing (`WriteNotApplied` must fire).
- A verification that compares in the wrong space or with a tolerance relaxed to make it pass.
- A check that cannot distinguish "nothing changed" from "nothing was read" (empty-set hash lesson).
- Live-write tests run without a restore step; a restore that is called complete while any declared
  captured channel was not compared (AUD-015); a live state case that does not prove untouched
  blocks byte-identical (AUD-001).
- A mutation tool/op whose default applies changes without an explicit request (AUD-009).
- A write path that does not revalidate target token + expected revision before each unit/batch
  (AUD-003/AUD-010), or that silently re-targets a replacement instance.
- A "verified rollback" claim without per-channel verification, or `FAILED_AND_RESTORED` reported
  while any captured channel is unverified (AUD-002).
- "Zero host writes" claimed while `write_calls_attempted > 0`; counters that cannot distinguish
  attempted / verified / unknown write effects (AUD-007).
- A cache write that overwrites a foreign/mismatched file instead of quarantining it, or a non-atomic
  cache write (AUD-012).
- A cache key that omits a behaviour-affecting provider determinant (AUD-008).
- An import of the legacy `_b4_lib` chain from Core or the provider (AUD-005).
- Any new wall-clock sleep used to "fix" flakiness.
