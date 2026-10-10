# 19 — Owner Decisions (Phase D Approval Document)

Status: **PHASE D — AWAITING OWNER APPROVAL.** Nothing in this document is approved yet. Every
decision below carries the status `OWNER_APPROVAL_REQUIRED` until the owner records otherwise in
`§6`. A choice is never represented as approved because the documentation recommends it.

This document is intentionally short. It contains **only** the decisions that genuinely affect
product scope, the existing server's behaviour, or accepted technical risk. Everything else has a
recommended V1 default, already adopted consistently across the specification (`§5`).

**Status vocabulary** (used here and in [15 §2](15-risks-and-open-questions.md)):

| Status | Meaning |
|---|---|
| `RESOLVED_BY_SOURCE` | answered by source inspection or an established project requirement; recorded with its evidence |
| `RECOMMENDED_DEFAULT` | routine engineering choice with a safe recommended position; adopted unless the owner objects |
| `DEFERRED_FROM_V1` | deliberately excluded from V1; may return in a later version with its own safety contract |
| `OWNER_APPROVAL_REQUIRED` | needs the owner's explicit decision before the specification is frozen |
| `OWNER_APPROVED` | the owner has explicitly approved it (none yet) |

---

## Decision 1 — V1 transaction recovery guarantee

**Status:** `OWNER_APPROVAL_REQUIRED` · **Blocks V1:** yes (it defines the writable surface).
Derived from Q-06; audit findings AUD-002/AUD-015.

**Question.** Approve the invariant that *every mutation must have a complete captured recovery set
and a verified restore path for every mutable channel the operation can change — otherwise the
operation is refused before the first write*?

**Recommended answer.** **Approve.** For the Vital provider this means before any write the
transaction captures, in one batched read set: the decoded state document, the route view, and the
**full parameter vector** (parameter values are the channel a document restore can silently reset —
the POC's measured behaviour). Restoration follows the provider-declared order (document →
parameters), and rollback success is reported **per channel**: `FAILED_AND_RESTORED` only when every
captured channel was read back and compared; otherwise
`FAILED_AND_PARTIALLY_RESTORED` / `FAILED_AND_NOT_RESTORED` / `ROLLBACK_FAILED`, with
`final_state: partially_restored` or `unknown`.

**Functional consequence (the part that needs approval).** If any plugin operation cannot satisfy
the guarantee, that operation is **outside the initial V1 writable surface**. In practice: no
component of V1 may ship a weaker "document-only" rollback; a future provider that cannot capture or
restore one of its mutable channels must either declare a narrower capture scope with a recorded
justification that the omitted channel cannot be changed, or have its mutating operations excluded
until the gap is closed.

**Alternatives.**
1. **Narrower V1 write scope** (fallback): expose parameter-only writes with full-vector capture, and
   defer document/route mutations to V2. Safe, but removes modulation and state features that are the
   point of the layer.
2. **Document-only capture with named gaps** — the POC's current contract. **Rejected by the audit
   (AUD-002):** a document restore resets parameters, so the rollback would not return the plugin to
   its pre-transaction state while still being described as a rollback.

**Trade-offs.** Full capture adds one batched in-process read before each mutation (the POC measures
batch parameter IO as a single in-process hop, so the read is cheap, but its exact cost is measured
in P5/P8 before the contract freezes: [02 §19.9](02-vital-poc-audit.md)). When a complete capture
cannot be obtained (unreadable channel, wedged host), the operation refuses — availability is
traded for recoverability, deliberately.

**Consequence of deferral.** Without this decision the write path has no honest rollback claim, the
transaction guarantees cannot be frozen, and P2/P8 cannot proceed. Deferral blocks V1.

**Affected documents.** [11 §2.3, §2.3a, §2.7, §3](11-planning-transactions-and-verification.md);
[05 §2.3, §2.8](05-universal-core-contract.md); [06 §7](06-mcp-tool-contracts.md);
[07 §2](07-capability-profile-schema.md); [12 §5 B-32…B-37, §7](12-testing-strategy.md);
[15 R-19](15-risks-and-open-questions.md); [18 AUD-002, AUD-015](18-remediation-register.md).

**Still required after approval (runtime, not documentation).** Capture cost measurement (P5/P8);
live per-channel restore proofs on the designated test project (P8).

---

## Decision 2 — Provider packaging and codec isolation

**Status:** `OWNER_APPROVAL_REQUIRED` · **Blocks V1:** yes (P7 cannot start without it).
Derived from Q-02; audit findings AUD-005/AUD-013; source evidence [17 AV-05, AV-13](17-planning-evidence-index.md).

**Question.** Where does the Vital provider live, and how is its codec isolated from the legacy
host-dependent code?

**Recommended answer.**
- **Provider code: in-repo, separately isolated package** —
  `src/reaper_reapy_mcp/plugins/providers/vital/`. It implements `PluginProvider`/`StateCodec`,
  imports **no** `reapy`, `reaper_controller`, `_bh_session`, `_b4_lib` or any legacy module, and is
  guarded by the recursive import-boundary test (V-12). This replaces the Phase-A "out-of-tree at
  `C:\REAPER\...`" position, which coupled the provider's runtime to the POC tree and complicated
  testing and packaging.
- **Codec: extract the pure, host-free algorithm** into a dependency-free module inside the
  provider package. Source verification ([17 AV-13](17-planning-evidence-index.md)) shows the
  extraction boundary exactly: in `_b4_lib.py` only `_get_chunk`, `_set_chunk` and `_default_ctrl`
  (plus the module-level aliases) touch the host; the byte codec and routing helpers —
  `_b64d`, `find_vst_block`, `parse_framing`, `decode_document`, `encode_item_b`, `encode_item_a`,
  `wrap_lines`, `rebuild_chunk`, `_dig`, `list_routings`, `find_free_slot`, `make_routing`,
  `clear_routing`, `export_document` — are stdlib-only and import nothing. They are ported (with the
  POC's byte-identity fixtures as the validation corpus; the POC measured exact round-trip fidelity)
  — never imported. Fallback if any function proves not host-free in practice: a clean
  provider-owned re-implementation from the documented layout, validated by the same fixtures.
- **Registry data: read from a configured data directory**, default the existing POC registry
  directory (`C:\REAPER\prompts\vital_sound_design\registry`), overridable via
  `REAPER_MCP_VITAL_REGISTRY_DIR`. Data is **not vendored** into the repo by default (it is generated
  from measured evidence; a copy would be a second source of truth with drift risk — the POC measured
  drift between regenerations). The profile records `provenance.source_digest` over the actual files
  read, so a stale or replaced directory can never be served silently. Offline tests use pinned
  fixture subsets, so the test suite needs no `C:\REAPER`.

**Alternatives.**
1. **Out-of-tree provider (Phase-A position).** Keeps the repo smaller, but the provider cannot be
   tested, packaged or versioned with the layer; already superseded by this recommendation.
2. **Clean re-implementation of the codec** (no extraction). Fully independent, but duplicates the
   byte-exact algorithm whose details (280-char wrap, trailing artifact, two length fields) were
   hard-won and fixture-verified — the highest-risk code in the system duplicated.
3. **Vendor the registry JSON into the repo.** Removes the data-directory dependency entirely;
   creates a second copy of generated data requiring a sync/refresh story. Acceptable if the owner
   wants full self-containment; not required.
4. **Separate distribution/repository for the provider.** Only justified if a real deployment
   requirement exists; the recommended in-repo package can later be split out if needed.

**Trade-offs.** In-repo code increases the repo's surface slightly but makes the provider subject to
the same boundary tests, reviews and releases as the layer; the data directory remains an external
dependency for *live* use (not for import, tests or server startup).

**Consequence of deferral.** Blocks P7 (provider) and therefore P8 (live mutation). Deferral leaves
the Vital provider unimplementable under the current contracts.

**Affected documents.** [08 §6, §7, §8](08-provider-and-host-interfaces.md);
[04 §2, §6](04-architecture.md); [03 matrix rows](03-component-responsibility-matrix.md);
[13 §4, §7](13-existing-mcp-integration.md); [14 P7](14-implementation-plan.md);
[15 R-02, R-03, R-18](15-risks-and-open-questions.md); [18 AUD-005, AUD-014](18-remediation-register.md);
[17 AV-13](17-planning-evidence-index.md).

**Additional packaging decision requested only if it matters to you:** whether vendor-in the
registry files (alternative 3) or keep the configured data directory (recommended). Otherwise the
recommended default stands.

---

## Decision 3 — Residual cross-process concurrency risk

**Status:** `OWNER_APPROVAL_REQUIRED` · **Blocks V1:** yes (it freezes the guarantee wording).
Derived from Q-12; audit finding AUD-010.

**Question.** Accept best-effort external-conflict detection — target-scoped locking plus
revision-based checks with fail-closed handling of observed conflicts — **acknowledging that the
design cannot prove atomic exclusion against every external writer**, because REAPER's APIs provide
no compare-and-swap?

**What the implementation can detect.**
- **Target identity change:** the write-target token is revalidated against fresh host facts before
  every state-write unit and before the parameter batch; a replacement or move refuses
  (`TargetChanged`).
- **State revision divergence:** the expected revision (document + route view + captured parameter
  vector) is compared against fresh reads before each unit and before rollback; an unexpected change
  refuses before the write (`StateConflict`), or triggers rollback mid-sequence.
- **Rollback conflicts:** each rollback write is preceded by its own re-check (below).

**How detected conflicts are handled.**
- Before any write: typed refusal with `write_calls_attempted: 0` for the refused unit; nothing is
  written and the caller is told what diverged.
- Mid-sequence: rollback of the transaction's own writes only.
- **Rollback never blindly overwrites externally changed state:** rollback restores only channels
  whose revision still matches the transaction's own last-verified revision; a channel showing
  external divergence is **left untouched** and reported as `not_restored (external_change)`, with
  `final_state: partially_restored` or `unknown`. A rollback is reported as verified only when every
  captured channel was read back and compared.

**What cannot be excluded (the residual risk being accepted).**
- A human editing the plugin GUI, or another script/process writing to the same instance, **between**
  the revision check and the write itself. The window is small but real.
- The plugin or REAPER itself changing state (preset load, GUI actions) after the transaction's
  verification read.
- **Why:** `GetTrackStateChunk`/`SetTrackStateChunk` and the `TrackFX_*` parameter APIs offer no
  transactional write or compare-and-swap; "check then write" cannot be made atomic from outside the
  host. No amount of application code removes the window; only a host-side primitive would.

**Recommended answer.** **Accept**, with the guarantees frozen as: *transactional with verified
compensation and best-effort external-conflict detection*. Detected conflicts always refuse or
report partial/unknown outcomes — never silently overwrite. The limitation is stated in
[11 §7](11-planning-transactions-and-verification.md) and [05 §5](05-universal-core-contract.md),
not buried.

**Alternatives.** (a) Block V1 writes until the host provides CAS — indefinite; no such API exists.
(b) Ship read-only V1 — safe but removes the product's purpose. (c) Accept (recommended).

**Consequence of deferral.** The transaction guarantee cannot be frozen and P2/P8 cannot pass their
exit criteria; writing "no claim we cannot keep" into the outcome schema requires the answer.

**Affected documents.** [11 §2.7, §6, §7](11-planning-transactions-and-verification.md);
[05 §5](05-universal-core-contract.md); [04 I-17](04-architecture.md);
[12 §5 B-50…B-54](12-testing-strategy.md); [15 R-08](15-risks-and-open-questions.md);
[18 AUD-010](18-remediation-register.md).

---

## Decision 4 — Existing REAPER MCP startup lifecycle

**Status:** `OWNER_APPROVAL_REQUIRED` (confirmation) · **Blocks V1:** no (Option A is the status quo).
Derived from the lifecycle question; audit finding AUD-006; source [01 §3](01-existing-system-audit.md) [A].

**Question.** Retain the existing **eager startup** — the MCP process requires REAPER to be running
because `BaseController.__init__` calls `reapy.connect()` — for V1? Or redesign for lazy connection
so the server can start with REAPER closed?

**Recommended answer.** **Retain eager startup (Option A).** Document the consequence: the server
process does not start when REAPER is closed; there is no promise that a tool can answer when the
process never started. If the connection is lost **after** startup, the affected call returns a
typed `NotConnected` (detectable: reapy raises; the bridge classifies — POC precedent). The plugin
layer itself adds no eager connection, does no host work at import/registration time, and is
internally lazy, so the offline test suite runs with REAPER closed.

**Alternatives.** **Option B — lazy connection lifecycle**: move the connection out of
`BaseController.__init__`. Consequences: every one of the 57 existing tools must degrade gracefully
without a connection (currently impossible — they are constructed after `connect()`); failure
semantics for existing tools change; a compatibility test matrix for all 57 tools is needed; startup
behaviour becomes a new support surface. This expands V1 well beyond "add plugin-aware tools" and is
justified only if a concrete product requirement establishes that the server must remain available
while REAPER is closed.

**Trade-offs.** Option A: zero changes to existing behaviour, smallest review surface. Option B:
more resilient startup, materially larger scope and risk.

**Consequence of deferral.** None for Option A (it is the existing state); deferral only means the
documented confirmation is pending. **If the owner requires Option B, the scope and phase plan must
be re-planned before P0 and this document re-issued** — it is a scope decision, not a task.

**Affected documents.** [01 §3](01-existing-system-audit.md); [06 §10](06-mcp-tool-contracts.md);
[13 §4, §6](13-existing-mcp-integration.md); [14 P0/P9](14-implementation-plan.md);
[15 R-09, Q-13](15-risks-and-open-questions.md); [18 AUD-006](18-remediation-register.md).

---

## 5. Not escalated — recommended defaults and source-resolved questions

The following were **not** brought to the owner: each has a safe recommended default or a
source/requirement-resolved answer, adopted consistently across the specification
([15 §2](15-risks-and-open-questions.md) records the full reasoning).

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

To approve, the owner marks each decision (edit this table; statuses are never changed by anyone
else):

| # | Decision | Status | Owner's note |
|---|---|---|---|
| 1 | Complete-capture-or-refuse transaction recovery; no weaker rollback ships | `OWNER_APPROVAL_REQUIRED` | |
| 2 | In-repo isolated Vital provider package; extracted host-free codec; configured registry data directory (or vendor-in, if preferred) | `OWNER_APPROVAL_REQUIRED` | |
| 3 | Accept residual external-race risk; guarantees frozen as transactional-with-verified-compensation | `OWNER_APPROVAL_REQUIRED` | |
| 4 | Retain eager startup (Option A); Option B only on explicit scope change | `OWNER_APPROVAL_REQUIRED` | |

**What approval means:** the specification in `docs/universal-plugin-mcp-v3/` is frozen as the basis
for implementation, and Phase E may begin at P0 under [14](14-implementation-plan.md) with the
mandatory gates.

**What approval does NOT mean:** that any runtime behaviour is proven. Every correction in
[18](18-remediation-register.md) is `CORRECTED_IN_DOCS`; the safety properties (targeting, recovery,
identity, concurrency) become claims only after their tests exist and pass — with live-write
validation gated at P8 and owner sign-off at P10.
