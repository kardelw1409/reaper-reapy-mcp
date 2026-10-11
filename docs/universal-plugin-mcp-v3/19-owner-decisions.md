# 19 — Owner Decisions and Approvals

Status: **PHASE E — FOUR DECISIONS `OWNER_APPROVED` (2026-10-10); PLANNING BASELINE APPROVED
PENDING FINAL OWNER SIGN-OFF.** The four decisions below are approved and binding. Everything else
in this document is an adopted default, a source-resolved answer or a V1 deferral (`§5`), and the
remaining limitations in `§7` are **technical limitations, not open decisions**.

**Approval scope.** The owner approved these four decisions — and only these. Approval is **not**
authorization to implement: implementation happens in a separate session, from the handoff in
[20-implementation-handoff.md](20-implementation-handoff.md), and it is **not** authorization to
assume approval for new scope, new safety compromises or unrelated architecture changes. A proposed
change that contradicts an approved decision must come back to the owner.

This document is intentionally short. It contains **only** the decisions that genuinely affected
product scope, the existing server's behaviour, or accepted technical risk.

**Status vocabulary** (used here and in [15 §2](15-risks-and-open-questions.md)):

| Status | Meaning |
|---|---|
| `RESOLVED_BY_SOURCE` | answered by source inspection or an established project requirement; recorded with its evidence |
| `RECOMMENDED_DEFAULT` | routine engineering choice with a safe position; adopted unless the owner objects |
| `DEFERRED_FROM_V1` | deliberately excluded from V1; may return in a later version with its own safety contract |
| `OWNER_APPROVAL_REQUIRED` | needs the owner's explicit decision before the specification is frozen (**none remain**) |
| `OWNER_APPROVED` | explicitly approved by the owner — the four decisions in this document (2026-10-10) |

---

## Decision 1 — V1 transaction recovery guarantee

**Status:** `OWNER_APPROVED` (2026-10-10) · **Defines:** the V1 writable surface.
Derived from Q-06; audit findings AUD-002/AUD-015.

**Question.** Approve the invariant that *every mutation must have a complete captured recovery set
and a verified restore path for every mutable channel the operation can change — otherwise the
operation is refused before the first write*?

**Approved position (binding).** Every V1 mutation captures the complete recovery set for every
mutable channel it may change **before the first write**. If the complete set cannot be captured, or
no supported restoration path exists for a channel, the operation **refuses before any mutation**.

For Vital, the provider-declared restoration sequence is fixed as four ordered steps:

1. Restore the selected document/state.
2. Restore all captured parameter values.
3. Verify the restored document/state.
4. Verify every restored parameter against its pre-transaction live value.

Rollback success is reported **per channel**: `FAILED_AND_RESTORED` only when every captured
channel was written back and compared; otherwise `FAILED_AND_PARTIALLY_RESTORED` /
`FAILED_AND_NOT_RESTORED` / `ROLLBACK_FAILED`, with `final_state: partially_restored` or `unknown`.

If an external change is detected, rollback does **not** blindly overwrite externally changed state:
it restores only channels whose revision still matches the transaction's expected state, reports the
conflicting channels as `not_restored (external_change)`, and returns the appropriate partial or
uncertain final-state outcome (§Decision 3).

**Terminology (binding).** The V1 protocol is **not** described as atomic anywhere. It is a
**transaction with verified compensation** and explicitly bounded concurrency guarantees
([11 §7](11-planning-transactions-and-verification.md)); "atomic" appears only inside a sentence that
immediately qualifies it.

**Technical consequences.** If any plugin operation cannot satisfy the guarantee, that operation is
**outside the V1 writable surface**. No component may ship a weaker "document-only" rollback. A
transaction captures, in one batched read set: the decoded state document, the route view, and the
**full parameter vector** (parameter values are the channel a document restore can silently reset —
the POC's measured behaviour, [17 AV-08, AV-12](17-planning-evidence-index.md)). Capture cost is
measured at P5/P8; the measurement informs the documented cost, not the guarantee.

**Alternatives considered and rejected** (recorded for provenance; **not open questions**):
1. **Narrower V1 write scope** (parameter-only writes with full-vector capture, document/route
   mutations deferred) — retained only as the fallback if P8 measures capture cost as prohibitive;
   that would require a new owner decision, because it changes the writable surface.
2. **Document-only capture with named gaps** — the POC's contract. Rejected (AUD-002): a document
   restore resets parameters, so the rollback would not return the plugin to its pre-transaction
   state while still being described as a rollback.

**Approval effect on planning.** P2 (mock transaction mechanics), P5 (codec/write contract) and P8
(live mutation) are authorized to proceed under this guarantee; P2's snapshot-or-refuse path and
P8's live per-channel restore proofs are where it is enforced. Deferral would have blocked V1's
write path entirely.

**Affected documents.** [11 §2.3, §2.3a, §2.7, §3, §7](11-planning-transactions-and-verification.md);
[05 §2.3, §2.8, §5](05-universal-core-contract.md); [06 §7](06-mcp-tool-contracts.md);
[07 §2](07-capability-profile-schema.md); [12 §5 B-32…B-37, B-53, §7](12-testing-strategy.md);
[14 P2/P5/P8](14-implementation-plan.md); [15 R-19](15-risks-and-open-questions.md);
[18 AUD-002, AUD-015](18-remediation-register.md); [20](20-implementation-handoff.md).

**Mandatory tests (specified now, run during implementation — P2/P5/P8).** B-32…B-37 (snapshot-or-refuse; document→parameter
order; document-reset restore-order fixture; per-channel rollback verification incl. partial and
failed), plus the P8 live cases: full-channel capture → write → verify → restore → verify restore on
one parameter, one enum, one state path, two routes, and an induced failure. No test in this
specification has been executed.

**Implementation gates.** No live mutation before the complete-capture contract passes deterministic
mock tests (P2) and no rollback is reported as verified unless every required, non-conflicting channel
was compared after restoration ([14 §Global phase gates](14-implementation-plan.md)).

---

## Decision 2 — Provider packaging and codec isolation

**Status:** `OWNER_APPROVED` (2026-10-10) · **Defines:** the provider package boundary and isolation policy.
Derived from Q-02; audit findings AUD-005/AUD-013; source evidence [17 AV-05, AV-13](17-planning-evidence-index.md).

**Question.** Where does the Vital provider live, and how is its codec isolated from the legacy
host-dependent code?

**Approved position (binding).**
- **Provider code: in-repo, architecturally separate package** —
  `src/reaper_reapy_mcp/plugins/providers/vital/` (referred to as `plugins/providers/vital/`). It
  implements `PluginProvider`/`StateCodec` and stays separate from Universal Core: Core never sees
  provider identifiers, provider semantics or block bytes.
- **Codec work derived from the verified host-free function set** documented in
  [17 AV-13](17-planning-evidence-index.md): `_b64d`, `find_vst_block`, `parse_framing`,
  `decode_document`, `encode_item_b`, `encode_item_a`, `wrap_lines`, `rebuild_chunk`, `_dig`,
  `list_routings`, `find_free_slot`, `make_routing`, `clear_routing`, `export_document` are
  stdlib-only and import nothing; the host-touching functions (`_get_chunk`, `_set_chunk`,
  `_default_ctrl`, module aliases) are **excluded**. The portable set is validated against the POC's
  byte-identity fixtures. If a function proves not host-free in practice, the fallback is a clean
  provider-owned re-implementation validated by the same fixtures — still a **new implementation**,
  never an import of the legacy module.
- **Import isolation (hard requirement).** The provider must not import the legacy `_b4_lib` or
  `_bh_session` stack; its import graph must not pull in `reapy` or `ReaperController`; importing the
  provider must not change the working directory. Enforced by the recursive import-boundary test
  (V-12), which fails the build rather than warning.
- **Registry data through the configured location** `REAPER_MCP_VITAL_REGISTRY_DIR` (resolution,
  validation and error handling specified in [08 §6](08-provider-and-host-interfaces.md)). Data stays
  outside the repo by default; the profile records `provenance.source_digest` over the files actually
  read.
- **No separate repository, distribution or packaging system for the provider in V1.**

**Explicit scope limit of this approval.** This decision approves the **package boundary and the
isolation policy**. It does **not** claim the codec has been ported or verified — that is P7 work, and
until then every statement about the provider's behaviour remains a specification, not a measurement.

**Technical consequences.** The provider becomes subject to the same boundary tests, reviews and
releases as the layer (in-repo, one review surface). Core's import-boundary rule is unchanged:
`plugins/host/reaper.py` remains the only `reapy` importer inside `plugins/`. The data directory stays
an external dependency for **live** use only — never for import, server startup or the offline suite
(pinned fixtures).

**Alternatives considered and rejected** (recorded for provenance; **not open questions**):
1. **Out-of-tree provider** at `C:\REAPER\Scripts\sound_design\vital_provider\` (Phase-A position) —
   rejected: the provider's runtime and tests would live outside the reviewed repository.
2. **Clean re-implementation with no extract lineage** — retained only as the per-function fallback;
   duplicating the byte-exact algorithm wholesale would duplicate the highest-risk code in the system.
3. **Vendoring the registry JSON** — acceptable later if full self-containment is wanted; it creates a
   second copy of generated data with a refresh story, so it is not the default.

**Approval effect on planning.** P7 (provider + codec isolation) and P8 (live mutation) may proceed
under this boundary; [14 P7](14-implementation-plan.md) is written against it. Core remains
plugin-agnostic and no second repository is created.

**Affected documents.** [08 §6, §7, §8](08-provider-and-host-interfaces.md);
[04 §2, §6](04-architecture.md); [03 matrix rows](03-component-responsibility-matrix.md);
[13 §4, §7](13-existing-mcp-integration.md); [14 P7](14-implementation-plan.md);
[15 R-02, R-03, R-18](15-risks-and-open-questions.md); [18 AUD-005, AUD-014](18-remediation-register.md);
[17 AV-13](17-planning-evidence-index.md); [20](20-implementation-handoff.md).

**Mandatory tests (specified now, run during implementation — P7).** V-12 clean-process import (no
`reapy`/`ReaperController`/`_bh_session`/`_b4_lib`, cwd unchanged), the codec byte-identity fixtures
against the ported functions, and grep-level assertions that the host-touching functions were not
ported. None has been run.

**Implementation gates.** No Vital live mutation before codec isolation and provider conformance
tests pass (P7 → P8); the import-boundary test is a build gate, not an advisory check.

---

## Decision 3 — Residual cross-process concurrency risk

**Status:** `OWNER_APPROVED` (2026-10-10) · **Defines:** the concurrency guarantee wording.
Derived from Q-12; audit finding AUD-010.

**Question.** Accept best-effort external-conflict detection — target-scoped locking plus
revision-based checks with fail-closed handling of observed conflicts — **acknowledging that the
design cannot prove atomic exclusion against every external writer**, because REAPER's APIs provide
no compare-and-swap?

**Approved position (binding).** The owner accepts best-effort external conflict detection rather
than a guarantee of cross-process atomic exclusion. The implementation must use:

- **target-scoped in-process locking** (keyed to the validated target token, never to a string or
  slot number);
- **session-scoped target validation** (the token is revalidated against fresh host facts);
- **mutable state revisions/digests** as the change signal;
- **revalidation before every state-write unit and before the parameter batch**, and conflict checks
  before rollback where possible;
- **truthful reporting** of partial or uncertain outcomes.

If an external change is detected, the system must **not** blindly overwrite the conflicting state.

**Distinguishing the transaction's own writes from external changes (binding).** The expected
revision advances **only** from the transaction's own verified read-back: after each verified unit the
new expected revision is derived from what that unit's read-back actually returned. External changes
are therefore anything that does not match the expected revision *or* a later fresh read — the
implementation must never treat its own verified write as a conflict, and must never adopt an
unverified read-back as the expected revision, or it would erase an external change from its own
model and later overwrite it.

**What can be detected.**
- Target identity change: the token is revalidated before every state-write unit and before the
  parameter batch; a replacement or move refuses (`TargetChanged`).
- State revision divergence: the expected revision (document + route view + captured parameter
  vector) is compared against fresh reads before each unit and before rollback; an unexpected change
  refuses before the write (`StateConflict`), or triggers rollback mid-sequence.
- Rollback conflicts: each rollback write is preceded by its own re-check of the channel it restores.

**What remains inherently uncertain.**
- A human editing the plugin GUI, or another script/process writing to the same instance, **between**
  the revision check and the write itself.
- The plugin or REAPER changing state (preset load, GUI action) after the transaction's verification
  read, or a change that leaves no observable digest difference in the read surfaces the layer can
  see.
- A second writer that ignores the in-process lock (the lock coordinates this server, not the world).
- **Why:** `GetTrackStateChunk`/`SetTrackStateChunk` and the `TrackFX_*` parameter APIs expose no
  transactional write and no compare-and-swap, so "check then write" cannot be made atomic from
  outside the host. Locks and revision checks **reduce** the race; no amount of application code
  removes it.

**What the system must do when a conflict is observed.**
- **Before any write:** typed refusal for the refused unit with `write_calls_attempted: 0`, the
  observed digests in the outcome, and nothing written.
- **Mid-sequence:** rollback of the transaction's own writes only, each preceded by its channel
  re-check.
- **During rollback:** restore only channels whose revision still matches the transaction's own
  last-verified revision; a channel showing external divergence is **left untouched** and reported as
  `not_restored (external_change)`, with `final_state: partially_restored` or `unknown`. A rollback is
  reported verified only when every captured channel was read back and compared.
- **Never**: a write to a stale token, a silent re-target, a best-effort overwrite of foreign state,
  or an unchanged/restored claim the evidence does not support.

**Terminology (binding).** The guarantee is *transactional with verified compensation and best-effort
external-conflict detection*. "Atomic" is never claimed; where the word appears it is immediately
qualified ([11 §7](11-planning-transactions-and-verification.md), [05 §5](05-universal-core-contract.md)).

**Alternatives considered and rejected** (recorded for provenance; **not open questions**):
(a) block V1 writes until the host provides compare-and-swap — no such API exists, so this is an
indefinite block; (b) ship read-only V1 — safe but removes the product's purpose.

**Approval effect on planning.** P2 (concurrency/revision mechanics on mocks), P3 (token issuance and
revalidation) and P8 (live conflict behaviour) may proceed with the guarantee worded as above.

**Affected documents.** [11 §2.7, §6, §7, §8](11-planning-transactions-and-verification.md);
[05 §5](05-universal-core-contract.md); [04 I-17](04-architecture.md);
[12 §5 B-50…B-54](12-testing-strategy.md); [14 P2/P3/P8](14-implementation-plan.md);
[15 R-08](15-risks-and-open-questions.md); [18 AUD-010](18-remediation-register.md);
[20](20-implementation-handoff.md).

**Mandatory tests (specified now, run during implementation — P2/P8).** B-50…B-54 (concurrent mutation on one target;
cancellation during a state write and during a parameter write; stale expected revision at rollback
time; conflicting rollback; incomplete host outcome), each asserting the refusal-or-truthful-outcome
behaviour above, plus the external-divergence-during-rollback case asserting `not_restored
(external_change)`. None has been run.

**Implementation gates.** No write to a stale or ambiguous target; no blind rollback over a detected
external change; no successful-rollback claim unless restoration of every required, non-conflicting
channel was verified; no final acceptance while post-write outcomes remain ambiguous
([14 §Global phase gates](14-implementation-plan.md)).

---

## Decision 4 — Existing REAPER MCP startup lifecycle

**Status:** `OWNER_APPROVED` (2026-10-10) · **Defines:** the startup/connection lifecycle.
Derived from the lifecycle question; audit finding AUD-006; source [01 §3](01-existing-system-audit.md) [A].

**Question.** Retain the existing **eager startup** — the MCP process requires REAPER to be running
because `BaseController.__init__` calls `reapy.connect()` — for V1? Or redesign for lazy connection
so the server can start with REAPER closed?

**Approved position (binding).** **Retain the existing eager connection/startup lifecycle for V1.**
The server startup is not redesigned to let the MCP process remain available while REAPER is closed,
and compatibility with the existing REAPER MCP tools is preserved unchanged.

**Documentation rule (binding).** The documentation must **not** promise that an invocation-level
`NotConnected` response can be returned if the MCP server failed to start before tool registration.
If the process never started, no tool ran and no envelope exists; the failure surfaces as a client-side
server-start failure. The plugin layer adds no eager connection of its own, does no host work at
import/registration time, and is internally lazy, so the offline test suite runs with REAPER closed.

**Disconnect after a successful start (specified behaviour).** If REAPER disconnects (or the reapy
connection dies) *after* the server has started, the affected call returns a typed
`NotConnected` error **with an outcome** carrying what was actually observed ([06 §2](06-mcp-tool-contracts.md),
[11 §3](11-planning-transactions-and-verification.md)):

| Where the loss is detected | `error` | `outcome` | `final_state` |
|---|---|---|---|
| Before any write was issued | `NotConnected` | `stage` = the stage that failed; `write_calls_attempted: 0` | `unchanged_verified` |
| After a write was issued but before read-back completed | `NotConnected` | `write_calls_attempted > 0`, `write_effects_unknown > 0` | `unknown` |
| After a unit was written **and** verified, before later units | `NotConnected` | per-unit record; rollback attempted; rollback outcome reported | `unknown` or `partially_restored` unless every channel was verified restored |
| During rollback | `NotConnected` (or `ROLLBACK_FAILED` when the restore itself failed) | `rollback.verified` false with the channel state that was actually compared | `partially_restored` / `unknown` |

A lost connection is never reported as `unchanged_verified` when a write was issued and its effect could
not be read back, and never as restored unless every required channel was compared.

**Alternatives considered and rejected** (recorded for provenance; **not open questions**):
**Option B — lazy connection lifecycle**: move the connection out of `BaseController.__init__`. It
would require every one of the 57 existing tools to degrade gracefully without a connection, change
their failure semantics, and add a compatibility matrix for all 57 — a scope change, not a task. It is
not approved for V1; if a concrete product requirement later demands it, it returns as a **new owner
decision with a re-planned phase sequence**.

**Approval effect on planning.** P0 documents the lifecycle; P9 demonstrates it with the
`UnsupportedProvider` / `NotConnected` behaviours and the unchanged 57 tools. No phase redesigns
startup, and no phase may add a promise the process cannot keep.

**Affected documents.** [01 §3](01-existing-system-audit.md); [06 §2, §10](06-mcp-tool-contracts.md);
[13 §4, §6](13-existing-mcp-integration.md); [14 P0/P9](14-implementation-plan.md);
[15 R-09, Q-13](15-risks-and-open-questions.md); [18 AUD-006](18-remediation-register.md);
[20](20-implementation-handoff.md).

**Mandatory tests (specified now, run during implementation — P9).** B-44 (post-startup loss ⇒ typed `NotConnected`
with the truthful outcome row above), P9's tool-list/registration checks, and the offline-suite
verification that the plugin layer imports and registers with REAPER closed. None has been run.

**Implementation gates.** No phase may change the startup lifecycle; no documentation may claim an
invocation-level response for a server that never started; a tool present in the list must answer a
typed result after a successful start (P9).

---

## 5. Not escalated — adopted defaults and source-resolved questions

The following were **not** brought to the owner, and the four approvals do not change them: each has a
safe adopted default or a source/requirement-resolved answer, consistent across the specification
([15 §2](15-risks-and-open-questions.md) records the full reasoning). Approving the four decisions above
does **not** convert these into approved changes of scope, safety or architecture — if one of them
ever needs to move, it needs its own decision.

| # | Item | Status | Adopted position |
|---|---|---|---|
| Q-01 | Mutator dry-run default | `RECOMMENDED_DEFAULT` | Every mutating tool/op applies nothing unless invoked with `dry_run: false`; omitting the argument causes zero writes |
| Q-03 | Unmeasured plugin versions | `RECOMMENDED_DEFAULT` | Writes refuse for versions outside the measured set (`VersionMismatch`); no override flag in V1 |
| Q-04 | Defects in the existing 57 tools | `RESOLVED_BY_SOURCE` | Source shows neither known defect (`add_fx` fake success, `verify_connection` always true) blocks V3 — the plugin path bypasses controllers; recorded as evidence, fixes are optional non-V3 maintenance |
| Q-05 | Registration without a provider | `RECOMMENDED_DEFAULT` | Register the 7 tools unconditionally through the normal integration point; calls return typed `UnsupportedProvider` after a successful server start (per Decision 4) |
| Q-07 | Cache location | `RECOMMENDED_DEFAULT` | Configurable, user-local default (`%LOCALAPPDATA%\reaper_reapy_mcp\cache\plugins`, `REAPER_MCP_PLUGIN_CACHE` override); no machine-specific absolute paths in Core |
| Q-08 | Raw-parameter escape hatch | `DEFERRED_FROM_V1` | Not exposed in V1; raw parameters remain the implementation layer |
| Q-09 | Persistent restore points | `DEFERRED_FROM_V1` | In-memory transactional snapshots only; file-based restore points need their own safety contract before returning |
| Q-10 | Staged user work in git | `RESOLVED_BY_SOURCE` | Established requirement: preserve `project_controller.py` staging and the untracked probe doc; no phase stages, reverts or includes them |
| Q-11 | VST2 Vital instances | `RECOMMENDED_DEFAULT` | Fail closed with a typed refusal; supported formats and versions are explicit in the profile `limits[]` |

---

## 6. Approval record

The owner approved the four decisions on **2026-10-10**. Recorded verbatim as their approved
positions, each with its technical consequences, affected documents, mandatory tests and
implementation gates above. Statuses in this table may only be changed by the owner.

| # | Decision | Status | Owner's note |
|---|---|---|---|
| 1 | Complete transactional recovery: capture the complete recovery set for every mutable channel before the first write, refuse if it cannot be captured; Vital restore sequence document → parameters → verify document → verify parameters; conflicting channels reported `not_restored (external_change)`; V1 protocol described as a transaction with verified compensation, never as atomic | `OWNER_APPROVED` (2026-10-10) | |
| 2 | In-repo isolated Vital Provider at `plugins/providers/vital/`, architected separately from Universal Core; codec derived from the verified host-free function set ([17 AV-13](17-planning-evidence-index.md)); no import of `_b4_lib`/`_bh_session`, no `reapy`/`ReaperController` in its graph, no working-directory change on import; registry data via `REAPER_MCP_VITAL_REGISTRY_DIR` with specified resolution/validation/error handling; no separate repository or distribution in V1. Approves the boundary and isolation policy — not a claim that the codec is ported or verified | `OWNER_APPROVED` (2026-10-10) | |
| 3 | Best-effort external concurrency protection accepted instead of cross-process atomic exclusion: target-scoped in-process locking, session-scoped target validation, mutable state revisions/digests, revalidation before state-write units and parameter batches, conflict checks before rollback where possible, truthful partial/uncertain reporting; no blind overwrite of a detected external change; the expected revision distinguishes the transaction's own verified writes from external changes | `OWNER_APPROVED` (2026-10-10) | |
| 4 | Preserve the existing eager REAPER MCP startup; no redesign to keep the process available while REAPER is closed; no documentation promise of an invocation-level `NotConnected` when the server failed to start before tool registration; specified error and final-state reporting for a disconnect after a successful start; existing REAPER MCP tools preserved | `OWNER_APPROVED` (2026-10-10) | |

**What this approval means:** the four positions above are binding for V1 implementation. The planning
package in `docs/universal-plugin-mcp-v3/` is the approved baseline, pending the owner's final sign-off
of the package as a whole ([20](20-implementation-handoff.md)).

**What this approval does NOT mean:**
- it is **not** authorization to implement in this session (implementation starts in a separate
  session from [20](20-implementation-handoff.md));
- it does **not** mark any runtime behaviour as proven: every correction in
  [18](18-remediation-register.md) remains `CORRECTED_IN_DOCS`, and the safety properties (targeting,
  recovery, identity, concurrency) become claims only when their tests exist and pass — live-write
  validation is gated at P8 and owner sign-off at P10;
- it does **not** approve the Option-B lazy-startup redesign, vendoring the registry data, persistent
  restore points, a raw-parameter escape hatch, or any other deferred/`RECOMMENDED_DEFAULT` item — those
  would each require a new decision;
- it does **not** authorize bypassing a gate: the gates in
  [14 §Global phase gates](14-implementation-plan.md) are conditions of this approval, not suggestions.

---

## 7. Unresolved technical limitations (limitations, not decisions)

These are **not** open decisions awaiting the owner, and they must not be presented as if approval
could settle them. They are properties of the host and of V1 scope; the implementation must surface
them and the tests must measure them. Each is recorded where it constrains the design.

| # | Limitation | Nature | How it is handled | Where |
|---|---|---|---|---|
| L-1 | REAPER's APIs expose no compare-and-swap or transactional write, so a check-then-write race with an external writer cannot be eliminated | host capability | best-effort detection, fail-closed refusals, truthful partial/unknown outcomes, no blind rollback (Decision 3) | [11 §7](11-planning-transactions-and-verification.md) |
| L-2 | The MCP process does not start when REAPER is closed; a server that never started returns nothing at invocation level | pre-existing lifecycle | documented consequence, no promise the process cannot keep, plugin layer adds no eager connection (Decision 4) | [13 §4, §6](13-existing-mcp-integration.md) |
| L-3 | Identical-build plugin replacement may be indistinguishable to the host; only measured host evidence decides | host evidence gap | **measured 2026-10-10** (P8 `p8_l3_identity__20261010T203101Z`): a repopulated slot reproduced the **same** handle, so a replacement is indistinguishable. Enforced: every *observable* difference refuses before the first write, and every response states that instance continuity is **not claimed** (`meta.identity_basis`). No test or document may claim such a replacement is detected | [12 §5 B-38](12-testing-strategy.md), [16](16-independent-review-checklist.md) |
| L-4 | Full parameter-vector capture cost is not yet measured (feasibility is evidenced, cost is not) | unmeasured | measured at P5 on the mock/captured corpus and at P8 live; the guarantee does not depend on the number | [11 §2.3](11-planning-transactions-and-verification.md), [17 AV-12](17-planning-evidence-index.md) |
| L-5 | V1 supports only measured plugin versions and formats (Vital 1.5.5 VST3); VST2 and unmeasured versions fail closed | V1 scope | typed refusals with the measured set in profile `limits[]`; no override flag | [07 §3.2](07-capability-profile-schema.md), Q-03/Q-11 (`§5`) |
| L-6 | Modulated/derived values cannot always be attributed: a parameter's live value may reflect modulation rather than its stored value | plugin semantics | the codec declares read/write authority per field; modulation depth is a declared capability, refusals are first-class data | [05 §4](05-universal-core-contract.md), [10](10-state-modulation-dependencies.md) |
| L-7 | State splice byte-fidelity is proven only on captured chunks until a live multi-block write is measured | unmeasured | P5 proves it on captured chunks; P8's live multi-block case is the acceptance gate | [12 §5 B-26…B-31](12-testing-strategy.md), [15 R-21](15-risks-and-open-questions.md) |
| L-8 | Multi-step structural writes are multiple host calls; a transport failure between them is observable but not preventable | host capability | per-unit records, per-channel verified rollback, `unknown` where the effect is unknowable | [11 §5](11-planning-transactions-and-verification.md) |
| L-9 | Vital's oscillator distortion-type **value space** is unmeasured (no enum members, `law: none`), so the provider cannot justify the DISABLE set's `"None"` off value for those three settings | unmeasured registry data | a structural change refuses with `disable_value_not_measurable` naming the target, instead of writing a display value into a numeric field; the fix is a measurement (or a decision to drop those targets), not a guess | [10 §2.3a](10-state-modulation-dependencies.md), [18 addendum](18-remediation-register.md) |
| L-10 | **Route creation is unavailable with Vital in V1**: the rule `route_into_off_filter` cannot be evaluated (the registry carries no destination families; the filter on/off flag is live-document state the projected world does not carry), so creating a `(source, destination)` pair is refused | missing measured input | the refusal is typed and explicit (`DependencyViolation` / `unevaluated: ["route_into_off_filter"]`, zero writes); removal, amount-only updates and pair-clearing writes stay available. Lifting it needs one of: a measured destination-family table in the registry, or a Core change handing the dependency hook the decoded document (the shape of the accepted P8 `document_version` hook) | [10 §2.3a](10-state-modulation-dependencies.md), [19 §9](19-owner-decisions.md), [18 addendum 3](18-remediation-register.md) |
| L-11 | **A session write-target token cannot be used for a mutation**: the token's claim (instance continuity) is unprovable on this host, so it is refused rather than honoured | host evidence gap (the L-3 measurement) | `IdentityContinuityUnprovable` before any host work, with the remedy in the refusal (address by location); reads keep the token; the guarantee for a location-addressed mutation is in-call (capture, revalidate, write, verify, restore) and is disclosed in `meta.identity_basis` | [05 §5](05-universal-core-contract.md), [19 §9](19-owner-decisions.md) |

---

## 8. Release-review decisions (2026-10-10, after P0–P10)

The owner reviewed the P8–P10 report and the independent audit and returns **five** decisions. They
are binding for the release-review remediation and are recorded here rather than in a commit message.

**Decision 5a — unevaluated Vital dependency rules.** Do not implement the three live-flag rules by
guesswork and do not change refusal behaviour for the appearance of completeness. Keep the honest
`evaluated: false` status; refuse **only** where the rule's subject is decidable and its deciding
input is not; disclose the rest. *Consequence implemented:* the FM-subject guard, the disclosure in
every mutation response, and the `disable_targets` refusal (L-9) - see
[10 §2.3a](10-state-modulation-dependencies.md).

**Decision 5b — the plugin-owned derived leaf.** Keep the limitation. It is never a write target, the
captured bytes are never written back blindly, and the exclusion is reported as the named gap
`derived_leaf_not_restored` on every path where a restore ran - including a restore that brought
everything else back. `verified` and `complete` are separate claims.

**Decision 5c — identity at replacement.** Keep the fail-closed posture **and** stop implying the
token settles it: what is decidable (caller-supplied identity, session token, build/handle/state
differences) is enforced with a typed refusal before the first write; what is not decidable (an
identical-build replacement) is disclosed as `not_claimed` and recorded as L-3. No new live
replacement experiment was run for this review.

**Decision 5d — `force`.** No override in V1: `force=true` is a typed refusal before any host work,
a wrong type for the argument is `InvalidValue`, and the schema documents it. Nothing in the response
may suggest a policy was overridden.

**Decision 5e — gap reporting.** One fact, one meaning: `derived_excluded` = mechanism, named gap =
consequence; both are checked against each other by tests; the gap must survive the Core → MCP
envelope boundary (tested at the registered tool).

---

## 9. Safety-gate correction (2026-10-11, after the release-review report)

Two contradictions remained between the fail-closed policy and the code, and the owner returned them
as a **correction** rather than a new phase. Both are binding.

**Decision 6a — `route_into_off_filter` is now a gate, not a disclosure.** Route changes were
allowed while the rule was declared but not evaluated. That is the exact shape a fail-closed layer
must not have, so creating a modulation `(source, destination)` pair is **refused before the first
write**, from every surface and through every representation:

- `routes_create[*]` (a new pair) and `routes_update[*]` that names a non-empty `source`/`destination`;
- a state write under the codec's route table that does not **clear** a pair field - including
  writing the whole table or a whole slot, so the state channel cannot bypass the route API;
- a projected world that counts creates without carrying their specs (not decidable ⇒ refused);
- the refusal is `DependencyViolation` with `detail.unevaluated: ["route_into_off_filter"]`, the
  per-finding `evaluated: false`, `write_calls_attempted: 0` and `REFUSED_BEFORE_ANY_WRITE` - and it
  is **identical for `dry_run: true` and `false`**, so a preview never presents it as applicable.

Operations whose safety provably does not depend on the rule stay available: `routes_remove[*]`,
`routes_update[*]` without source/destination, a state write that clears a pair field, state writes
outside the route table, and every read-only operation. The guard is derived from the codec's own
layout (`parse_route_pair_path` / `addresses_route_table`) rather than from a hand-typed name list,
and a test ties the two together. **No destination mapping was invented**, and the rule is not
presented as evaluated: it is refused. Consequence recorded as **L-10**: with Vital's rule table,
route *creation* is unavailable until the deciding inputs exist (a measured destination-family table,
or a Core change handing the dependency hook the live document - the same shape as the accepted P8
version hook).

**Decision 6b — a token-carrying mutation is refused.** `not_claimed: ["instance_continuity"]` is a
disclosure, not a protection. A caller-supplied write-target token claims *the instance that was
validated*, and the host cannot prove that claim (measured: an identical-build replacement reproduces
handle and the same build facts; the artifact records no exposed datum that separates them). The layer therefore refuses such a mutation with
`IdentityContinuityUnprovable` **before any host work**, for every mutation on both surfaces, and
states the remedy: address the instance by location (or by the `data.instances[]` object), which is a
present-tense claim it can verify inside the call. Reads keep accepting the token; a token whose
facts disagree is still `TargetChanged`.

**Which mutations remain available, and on what basis** (the owner asked for this explicitly):

| Mutation | Available? | Basis |
|---|---|---|
| parameter write, location-addressed | yes | in-call: complete capture, per-unit revalidation, write, verified read-back, verified restore |
| state write to a non-route path, location-addressed | yes | same |
| route removal / amount-only update / pair-clearing write | yes | the unevaluated route rule's subject (a created pair) is not entered |
| route creation (any spelling) | **no** | the rule is unevaluable → Decision 6a |
| any mutation carrying a token or an uncomparable identity claim | **no** | continuity is unprovable → Decision 6b |
| every read-only operation | yes | reads make no write claim |

No new live experiment was run for this correction: it is reproduced in mocks from the existing L-3
evidence (`p8_l3_identity__20261010T203101Z`) and the measured rule table.
