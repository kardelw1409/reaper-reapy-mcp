# 11 — Planning, Transactions, Verification and Rollback

Status: **[D] proposed** (rev. 2026-10-10, Phase C remediation → Phase D consolidation → Phase E approvals and handoff — see [18](18-remediation-register.md), [19](19-owner-decisions.md), [20](20-implementation-handoff.md)),
extracted from the POC's measured transaction engine [A: 02 §12] **as corrected by the audit**: the
snapshot is now complete-or-refuse across every mutable channel (AUD-002), state writes are
slice-exact and target/revision-revalidated (AUD-001/AUD-003/AUD-010), and the outcome schema
separates the error from the recovery state (AUD-007).

## 1. The transaction state machine

```mermaid
stateDiagram-v2
    [*] --> Preflight
    Preflight --> Refused: invalid input / unknown names / bad units / stale target
    Refused --> [*]: error + outcome{REFUSED_BEFORE_ANY_WRITE, final_state: unchanged_verified}
    Preflight --> Plan: pure resolution OK; target token issued
    Plan --> Refused: hard dependency / plan error / revision conflict
    Plan --> Snapshot: plan built against live state
    Snapshot --> Refused: capture incomplete or unverifiable (AUD-002)
    Snapshot --> DryRun: dry_run=true (default)
    DryRun --> [*]: preview (would_write); NO_WRITES/unchanged_verified
    Snapshot --> Apply: dry_run=false (explicit apply)
    state Apply {
        [*] --> Revalidate
        Revalidate --> StateUnit: token + expected revision OK
        Revalidate --> Rollback: stale token or revision conflict
        StateUnit --> VerifyState: slice re-read + decode + compare
        VerifyState --> StateUnit: more ordered units (re-read live slice first)
        VerifyState --> ParamBatch: all state units verified
        VerifyState --> Rollback: mismatch / write raised
        ParamBatch --> VerifyParams: one hop, ident-addressed
        VerifyParams --> Rollback: mismatch / write raised / wedge
    }
    VerifyParams --> Commit: every item verified; revision advanced from own read-back
    Commit --> [*]: ALL_VERIFIED / final_state: committed + per-item read-back
    Rollback --> [*]: FAILED_AND_RESTORED | PARTIALLY | NOT_RESTORED | ROLLBACK_FAILED
```

The seven stages, and nothing may skip one: **preflight → plan → snapshot → (dry-run) →
revalidate/apply (ordered units) → verify → commit | rollback**.

## 2. Stage contracts

### 2.1 Preflight (no host access)

- Resolve instance ref → profile (cache path, [09](09-discovery-and-cache.md)); issue the
  session-scoped **write-target token** ([05 §5](05-universal-core-contract.md)); a stale or
  ambiguous resolution is refused here.
- `provider.normalize_request`: names (control/module/route/state paths) → bindings; display values →
  typed values; vocabulary checks; range/enum/unit validation. Failures are **refusals with
  suggestions**, and the response proves it: `outcome.host.write_calls_attempted == 0` [A: POC `submit`].
- An actionable refusal, never a silent no-op: per-item problems with legal values/ranges [A].

### 2.2 Live read + Plan (reads only)

- Read exactly what the plan needs: live route table (all 64 slots when routes are involved),
  affected flags/states, and — only at parameter-write time — the planned parameter indices.
  No full sweeps [A: 02 §7 rationale].
- **Projection:** evaluate dependencies against live state + planned changes, not against the raw
  snapshot [A: POC `projected_flags`; recorded false-refusal defect].
- Plan items each name a channel; a plan is data; building it writes nothing [A: `plan.build`].

### 2.3 Snapshot — complete capture or refuse (Phase C, AUD-002)

**Contract: every mutable channel that this operation can change is snapshotted and verifiably
restorable before the mutation is permitted. If a complete, verifiable capture cannot be obtained,
the mutation is refused before the first write (`SnapshotUnavailable`, `write_calls_attempted: 0`).**

Captured channels (Vital V1; `provider.capture_scope()`):

| Channel | Content | Why it must be captured |
|---|---|---|
| **State document** | full decoded document (deep copy) + framing reference + the slice/span it came from | state changes and route fields write here |
| **Routes** | all route slots (snapshot/restore view) | route mutations write here |
| **Parameters** | `{ident → normalized}` for the **full parameter capture scope** — the vector a document restore can reset, which is *not* limited to the controls the request names | a document write resets ordinary parameters to the document's snapshot values (measured), so restoration is incomplete without them (AUD-002/AUD-015) |
| **Base revision** | `StateRevision` at capture time | conflict detection during apply/rollback |

Rules:

- The parameter capture is one batched in-process read ([08 §4.3](08-provider-and-host-interfaces.md));
  its measured cost is verified in P5/P8 before the contract freezes ([02 §19.9](02-vital-poc-audit.md)).
  A provider may declare a *smaller* capture scope only with a recorded justification that the
  omitted channel cannot be changed by the operation (e.g. a parameter-only provider without a
  document channel); the default is complete capture.
- Every captured value is read **from the authoritative channel** (parameters from the parameter
  array — never from the document; the POC's §6.3 rule) and ident-validated on read-back.
- Snapshot is **memory-only during the transaction** — no file is written (POC precedent: no file
  I/O in `transaction.py` [A]).
- **Persistent restore points are a separate artifact and are deferred from V1** (AUD-009). If ever
  reintroduced, they carry the same explicit channel manifest + content hash, and the audit's file-
  safety requirements apply ([10 §1.3](10-state-modulation-dependencies.md)). A transaction never
  promotes its snapshot to a restore point; a run that finds no recorded `before` state refuses
  rather than comparing against itself [A: evidence lesson, [17 E-16](17-planning-evidence-index.md)].
- The manifest reports `captured` and (for reads/diagnostics) `not_captured`; for **writes**, a
  non-empty `not_captured` within the capture scope is a refusal, not a footnote.

### 2.3a Restoration order (provider-declared; Vital V1)

```
1. restore the state document (through the slice; verified)
2. restore the captured parameter values (one ident-addressed batch; verified)
3. re-verify the document (nothing later writes it)
4. verify every restored parameter against its pre-transaction live value
```

Why this order: a document write resets ordinary parameters to the document's values, so parameters
must be rewritten **after** the document, and the document must be the last state channel checked.
Providers declare their own order where the mechanism differs; the engine enforces whatever the
provider declares and verifies each channel that was captured.

### 2.4 Apply (ordered, provider-declared, engine-enforced)

1. **Order is not caller-configurable.** The engine enforces `provider.write_order()`. A reversed
   order is refused (`WriteOrderViolation`) — the POC ships a probe solely to prove the refusal
   [A: transaction.py:607-622].
2. **Vital's declared order: state/document first, parameters second.** Measured reason: a document
   write whose content differs from the stored document resets every ordinary parameter to the
   document's snapshot values (cutoff 0.20→0.54688) [A][C].
3. **Per-unit revalidation (AUD-003/AUD-010).** Immediately before each `StateWriteUnit` and before
   the parameter batch: the target token is revalidated against fresh `FxHostFacts`, and the expected
   `StateRevision` is compared against a fresh read. Stale/mismatch ⇒ `TargetChanged`/`StateConflict`
   — before that unit's write (zero writes for this unit); mid-sequence ⇒ rollback of completed units.
4. **Units are slice-exact and sequential** when the codec declares a structural limit: Vital
   routes = one per state unit, each preceded by a live slice re-read (measured: 8/8 sequential vs
   0/2 batched) [A][C]. Untouched blocks are never rewritten (`TargetedStateSlice` splice).
5. **The lock** is held from final resolution through the complete verified outcome and recovery
   path, keyed to the validated target token (not a slot number) [AUD-010].

### 2.5 Verify (success definition)

**SUCCESS = VERIFIED READ-BACK on every written item, in the space that was written.**

- State: re-decode and compare each changed path; float tolerance 1e-5 default, non-float exact,
  plus any caller/provider predicate [A].
- Parameters: re-read and compare normalized values; tolerance 1e-3 default [A].
- **Space correctness is a normative rule**: the requester's display value is re-resolved through the
  *same* conversion the write used before comparison. The POC records the original defect: comparing
  normalized read-backs against display requests produced three false mismatches on a perfectly
  landed batch; "the fix is the SPACE, not the tolerance" [A].
- The verify result is data: `{ok, problems, channels_checked, per-item rows}` with reason strings —
  and the sentence that defines the system: *"success is a verified read-back, never an API return
  value"* [A].

### 2.6 Commit

Commit happens only if every written item verified. The response carries the batch's own read-back,
so a caller never needs a follow-up read (`read_cost_next: {needed: 0, why: …}` [A]).

### 2.7 Rollback — verified per captured channel (Phase C, AUD-002)

- Triggered by any apply/verify failure, a pre-unit revalidation failure mid-sequence, or an
  explicit cancellation that arrives after a write was issued.
- Restores in the provider-declared order (**document → captured parameters**, §2.3a), each as
  changes through the codec and host path — never a blind blob write.
- **Every captured channel is verified after restoration**, in its own comparison space. The outcome
  reports exactly what was verified per channel:
  * `FAILED_AND_RESTORED` — *every* captured channel verified at its pre-transaction value;
  * `FAILED_AND_PARTIALLY_RESTORED` — some channels verified restored, some not;
  * `FAILED_AND_NOT_RESTORED` — no captured channel verified restored;
  * `ROLLBACK_FAILED` — the restore attempt raised or produced an unverifiable read-back (the loud
    case; the caller must not be able to mistake this state for safe [A: POC `RollbackFailed`]).
- **An attempted rollback is never reported as a verified one** (AUD-007). `FAILED_AND_RESTORED`
  may not appear unless every declared captured channel was compared after restoration (AUD-002
  acceptance; AUD-015 applies the same rule to live-test evidence).
- Rollback verification tolerances mirror the primary verification; the POC executor's
  both-channels restore check (display equality **and** normalized within 1e-6) is the precedent for
  the parameter channel [A].
- A rollback itself revalidates the target token first; if the token is stale at rollback time, the
  outcome is `ROLLBACK_FAILED` with the reason — not a best-effort write to a possibly-wrong instance.
- **Revision interactions during rollback:** the rollback's own writes advance the expected revision
  from their read-backs, exactly like forward units.
- **Rollback never overwrites externally changed state** (Phase D, [19 Decision 3](19-owner-decisions.md)):
  before each rollback write the channel's live value is compared with the transaction's own
  last-verified revision for that channel. A channel that still matches is restored; a channel
  showing external divergence is **left untouched** and reported as
  `not_restored (external_change)`. Consequences: `final_state` is `partially_restored` when some
  channels were restored and others were left alone, and `unknown` when no honest aggregate exists;
  `FAILED_AND_RESTORED` requires that every captured channel was **written back and verified**. An
  externally changed channel is foreign state, not the transaction's to overwrite — the same rule
  that makes the cache quarantine a foreign file instead of replacing it ([09 §5](09-discovery-and-cache.md)).

## 3. Verdict and final-state vocabulary (normative, Phase C)

| Verdict | Meaning | `final_state` | Result shape |
|---|---|---|---|
| `ALL_VERIFIED` | every item written and verified; committed | `committed` | `ok: true` + per-item read-back + `outcome` |
| `REFUSED_BEFORE_ANY_WRITE` | preflight/plan/snapshot/token/revision refusal | `unchanged_verified` (write_calls_attempted == 0) | `ok:false` + typed error |
| `REFUSED_AFTER_A_WRITE` | an op-stream refused after an earlier unit committed, with rollback verified | `unchanged_verified`; without verified rollback → `unknown` | partial-commit report; not reachable in V1 single-request transactions (kept for op-stream compatibility) |
| `FAILED_AND_RESTORED` | failure; **every captured channel** verified restored | `unchanged_verified` | `ok:false`, `rollback.verified: true`, per-channel evidence |
| `FAILED_AND_PARTIALLY_RESTORED` | failure; some captured channels verified restored | `partially_restored` | per-channel booleans |
| `FAILED_AND_NOT_RESTORED` | failure; no captured channel verified restored | `unknown` | loud, with reasons |
| `ROLLBACK_FAILED` | restore raised or unverifiable | `unknown` | **loudest**; never folded into "ok:false" |
| `NO_WRITES` | dry-run, nothing written by design | `unchanged_verified` | `dry_run: true` |

Unverified restore outranks everything in the report [A: executor `_verdict`]. Each verdict maps to
exactly one `final_state`, and the mapping is test-enforced.

## 4. Failure taxonomy → caller-visible contract (Phase C, AUD-007)

Every row carries both the typed `error` and the complete `outcome` block ([05 §2.8](05-universal-core-contract.md)).

| # | Situation | Caller sees |
|---|---|---|
| F1 | invalid request/names/values | refusal, typed problems, `write_calls_attempted: 0` |
| F2 | unmet hard dependency | refusal with finding rows, `write_calls_attempted: 0` |
| F3 | plan cannot be built against live state (table shape changed) | refusal + the live state it resolved against [A] |
| F3b | snapshot incomplete/unverifiable | `SnapshotUnavailable`, `write_calls_attempted: 0` (AUD-002) |
| F3c | stale target token, or ambiguous slice correlation | `TargetChanged` / `TargetSliceAmbiguous`, `write_calls_attempted: 0` |
| F3d | expected revision conflicts with live state | `StateConflict`, `write_calls_attempted: 0` (mid-sequence: rollback first) |
| F4 | write issued, not applied (read-back mismatch) | `WriteNotApplied` + per-change classification + rollback outcome |
| F5 | verification mismatch after apply | same as F4, `stage: verify` |
| F6 | rollback verified on all captured channels | `FAILED_AND_RESTORED` / final_state `unchanged_verified` |
| F6b | rollback verified on some channels | `FAILED_AND_PARTIALLY_RESTORED` / `partially_restored` |
| F7 | rollback unverified/failed | `ROLLBACK_FAILED` / `unknown`, explicit |
| F8 | host transport failure pre-write | `NotConnected`/`Wedged`/`Unreadable`/`HostContractUnavailable`; write calls may be 0 |
| F9 | transport failure or wedge mid-write | `Wedged` + `write_effects_unknown > 0`; the outcome says `unknown`, **never "unchanged"** |
| F10 | connection loss after a verified unit | per-unit record; rollback attempted; outcome reflects the last verified unit |
| F11 | cancellation after a write was issued | truthful final-state report (`unknown` unless the unit completed and verified) — stopping is not assumed to have prevented mutation (AUD-010) |
| F12 | preflight refusal **after** reads (live table invalidated the plan) | reads counted, `write_calls_attempted: 0` |

The POC's honesty rule is kept and sharpened: the response says *which* of these happened, with
counters; nothing is folded into a generic error; "zero host writes" is only claimed when the
contract can establish that no write was invoked.

## 5. Partial failures and batch semantics

- **One logical request, many items.** The engine does not promise atomic success across items; it
  promises: every item is either verified-committed or explicitly reported, and a failure triggers
  one rollback covering everything the transaction wrote.
- **Stops at the first failing item** (POC executor: stream stops; refusal rows carry
  `writes_performed_before_this_refusal` [A]). V1 does not attempt partial success beyond what
  rollback restores; the rollback result is the authoritative final state report.
- **Multi-unit state writes** (Vital routes) are sequential; if unit 3 fails, units 1-2 are rolled
  back through the routed snapshot [A: mid-sequence `WriteNotApplied` → rollback path].

## 6. Ordering, staleness, retries, cancellation

- **Ordering:** fixed by provider declaration; verified in code by a negative test.
- **Staleness (three separate checks, AUD-003):** profile fingerprint compare-on-use (does the host
  still show the intended build?); target-token revalidation before each write unit and the parameter
  batch (is it still the intended instance?); expected-revision comparison against fresh reads (is
  the mutable state what we planned against?). Each failure has its own typed error and refuses;
  none silently re-targets.
- **Retry safety:** the transaction is **not idempotent** by declaration (a write may partially
  land); therefore retries are caller-visible and post-outcome: the engine either returns a verified
  rollback (safe to re-issue after re-reading) or a loud failed/uncertain outcome (do not re-issue;
  inspect). This is what the outcome schema exists to make decidable.
- **Settle/retry timings** are provider-declared (Vital: settle 0.4 s; retry once at 2-3 s), supplied
  by the engine from the provider's timing profile — **never caller-controlled** (AUD-009 removes the
  earlier `settle_s` tool parameter), and injected for tests — never wall-clock sleeps in unit tests [A].
- **Cancellation (AUD-010):** cancellation is a transition to a *truthful final-state report*, not
  an assumption that stopping execution instantly prevented mutations. If no write was issued:
  `REFUSED_BEFORE_ANY_WRITE`/`unchanged_verified`. If a unit was in flight: the outcome reports
  `write_effects_unknown` or the verified result, and a rollback is attempted.

## 7. Atomicity and external concurrency — what we claim, and what we do not (AUD-010)

- We do **not** claim host-level atomicity: `SetTrackStateChunk` is a single call whose effect its
  return value does not guarantee, and multi-unit structural writes are multiple calls.
- We do **not** claim cross-process exclusion: the host API provides **no proven compare-and-swap**,
  so revision checking plus the target-token lock **reduce but cannot eliminate** races with an
  external writer (another script, a human edit, a second process). The residual window is between
  the revision check and the write itself.
- We **do** claim: *transactional with verified compensation* — complete-or-refuse capture before,
  slice-exact target-aware writes, verification in the written space after, and a per-channel
  verified best-effort rollback whose outcome (fully / partially / not restored / rollback failed)
  is always reported. "Atomic" without the qualifier is forbidden in all V3 documentation.
- Best-effort conflict detection behaviour: revision mismatch observed ⇒ refuse before the unit; or
  rollback mid-sequence; both are reported with the observed digests. **The residual race window is
  accepted by the owner in [19 Decision 3](19-owner-decisions.md)**; rollback restores only channels
  whose revision still matches and reports `not_restored (external_change)` for the rest (§2.7).
- **What the limitation means for a caller:** a plugin instance is writable safely by one writer at a
  time in the supported topology; concurrent GUI edits or a second process are detected when they
  become visible to a read, never overwritten during rollback, and always reported.

## 8. Required tests (summary; full matrix in [12](12-testing-strategy.md))

| Area | Tests |
|---|---|
| Preflight | refusal ⇒ `write_calls_attempted == 0`; suggestions present; range/enum/unit rows |
| Plan | projected-world fixture; unrooted path refusal; live-state attachment on F3 |
| Snapshot (AUD-002) | complete capture manifests; **capture failure ⇒ 0 writes**; document-reset fixture (a document restore must be followed by parameter restoration) |
| Targeting (AUD-001) | multi-block chunk: intended block changes; untouched blocks byte-identical; ambiguous correlation ⇒ 0 writes (B-26…B-31) |
| Identity/revision (AUD-003) | token revalidation per unit; stale token ⇒ refusal, never re-target; revision advance after own write; external-change conflict (B-38…B-43) |
| Order | reversed-order refusal regression; document-before-parameters proven by mock call order |
| Verify | space-correctness regression (display vs normalized fixture); tolerances; per-item rows |
| Rollback (AUD-002/015) | per-channel verified; partial and failed restorations distinguished; parameters restored **after** document and verified; `FAILED_AND_RESTORED` withheld while any channel unverified |
| Verdicts | all 8 verdicts reachable; verdict↔final_state mapping holds |
| Concurrency/cancellation (AUD-010) | concurrent mutation; cancellation during state/parameter writes; stale revision; conflicting rollback; incomplete host outcome (B-50…B-54); **external divergence during rollback ⇒ that channel untouched and reported `not_restored (external_change)`** ([19 Decision 3](19-owner-decisions.md)) |
| Retry/settle | injected timing; wedge mid-write ⇒ `write_effects_unknown > 0`, final_state `unknown` |
| Honesty | API-returns-success-but-stores-nothing mock ⇒ `WriteNotApplied` (never success); zero-write claims only at `write_calls_attempted == 0` |
