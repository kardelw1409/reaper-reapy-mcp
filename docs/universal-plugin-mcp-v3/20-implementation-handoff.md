# 20 — Implementation Handoff (read this first)

Status: **APPROVED PLANNING BASELINE — PENDING FINAL OWNER SIGN-OFF. IMPLEMENTATION NOT STARTED.**
The four V1 decisions are `OWNER_APPROVED` (2026-10-10, [19 §6](19-owner-decisions.md)); the package
as a whole is awaiting the owner's final sign-off. Nothing in P0–P10 has been executed, and no test in
this documentation set has been run.

This document is the **entry point for a separate implementation session**. It is written so that the
Implementer does not need the planning conversation: everything below is either self-contained or
points at the exact normative document.

---

## 1. What is being built, and why

A **plugin-aware semantic layer** added to the *existing* REAPER MCP server — not a second MCP server.
It gives an LLM/agent a stable, provider-agnostic way to describe, read and write **one specific
plugin instance** inside REAPER (first provider: Vital 1.5.5 VST3), with preflight, planning,
transactional writes, verified read-back and per-channel verified recovery.

```
LLM / Agent → existing REAPER MCP (57 host-level tools, unchanged)
            → 7 new plugin_* tools (Universal Core)
            → Provider (Vital, in-repo, isolated)
            → Host Bridge (the only reapy importer inside plugins/)
            → VST3 plugin instance in REAPER
```

The existing server keeps ownership of host-level concerns (tracks, FX chain, items, MIDI,
transport, projects, rendering). Why it exists: the POC proved the semantics are achievable but also
proved the failure modes that must not ship — silent success, unprotected multi-VST targeting,
document-only "rollback", unverified writes ([02](02-vital-poc-audit.md), [11](11-planning-transactions-and-verification.md)).

**V1 deliverable:** 7 tools (`plugin_instances`, `plugin_describe`, `plugin_find`, `plugin_read`,
`plugin_write`, `plugin_state` (inspect-only), `plugin_modulation`) registered unconditionally on the
existing server, bringing the total to **64 tools**, plus the Universal Core, the Host Bridge, and the
Vital provider package.

## 2. Where everything lives

| What | Path |
|---|---|
| Target repository (implementation target) | `C:\repos\reaper-reapy-mcp` |
| Planning documentation (this package) | `C:\repos\reaper-reapy-mcp\docs\universal-plugin-mcp-v3\` |
| POC — the source of proven behavior (read-only) | `C:\REAPER\Scripts\sound_design\vital\` |
| POC data (registry, laws, evidence, architecture) | `C:\REAPER\prompts\vital_sound_design\` |
| New code (to be created in P0–P9) | `C:\repos\reaper-reapy-mcp\src\reaper_reapy_mcp\plugins\` |
| New tests (to be created in P0–P10) | `C:\repos\reaper-reapy-mcp\tests\plugins\` |

> The brief sometimes refers to the planning documents as living at
> `C:\repos\reaper-reapy-mcp\universal-plugin-mcp-v3\`. The actual, verified location is
> `docs\universal-plugin-mcp-v3\` (21 Markdown files, numbered 00–19 plus `README.md`). That is the
> approved location; nothing was moved.

**Repository facts the Implementer needs.** Package `reaper_reapy_mcp` (src layout, `pyproject.toml`
version 0.1.3, Python `>=3.10`, launched with `py -3.11`). Dependencies already present: `python-reapy`,
`python-osc`, `pytest`, `mcp[cli]`, `python-dotenv`, `requests` — **no new dependency is approved for
V1**. Server entry: `src/reaper_reapy_mcp/server.py` → `create_server()` → `ReaperController(debug=True)`
(eager `reapy.connect()`) → `FastMCP("Reaper Control")` → `setup_mcp_tools(mcp, controller)` →
`main().run()` (stdio). The 57 existing tools live in `src/mcp_tools.py`. Client config:
`C:\REAPER\.vscode\mcp.json` (server "reaper": `py -3.11 -c "import sys; sys.path.insert(0, r'C:\repos\reaper-reapy-mcp\src'); from reaper_reapy_mcp import main; main()"`).

## 3. Approved V1 decisions — binding, do not re-litigate

| # | Decision | Approved position (short form) |
|---|---|---|
| 1 | **Complete transactional recovery** | Capture the complete recovery set for every mutable channel before the first write; refuse if it cannot be captured. Vital restore sequence: document → parameters → verify document → verify parameters. Conflicting channels are reported `not_restored (external_change)`. The protocol is a **transaction with verified compensation**, never described as atomic. |
| 2 | **In-repo isolated Vital Provider** | Provider package `plugins/providers/vital/`, architecturally separate from Core; codec derived from the verified host-free function set ([17 AV-13](17-planning-evidence-index.md)); **no** `_b4_lib`/`_bh_session` import, **no** `reapy`/`ReaperController` in its import graph, **no** working-directory change on import; registry data via `REAPER_MCP_VITAL_REGISTRY_DIR` with the specified resolution/validation/error behaviour ([08 §6](08-provider-and-host-interfaces.md)); **no** separate repository or distribution in V1. Approves the boundary and isolation policy — **not** a claim that the codec is ported or verified. |
| 3 | **Best-effort external concurrency protection** | Target-scoped in-process locking, session-scoped target validation, mutable state revisions/digests, revalidation before state-write units and parameter batches, conflict checks before rollback where possible, truthful partial/uncertain reporting. No blind overwrite of a detected external change. The expected revision advances **only** from the transaction's own verified read-backs. |
| 4 | **Preserve eager REAPER MCP startup** | The process requires REAPER at start; no redesign to stay available while REAPER is closed; **no documentation promise of an invocation-level `NotConnected` when the server failed to start before tool registration**; a disconnect after a successful start yields a typed `NotConnected` with the truthful outcome row in [19 Decision 4](19-owner-decisions.md); existing tools preserved unchanged. |

Full text, technical consequences, mandatory tests and gates: [19-owner-decisions.md](19-owner-decisions.md).

## 4. What is out of V1 (do not build it)

`DEFERRED_FROM_V1`: raw-parameter escape hatch; persistent/file-based restore points.
`RECOMMENDED_DEFAULT` (adopted, not to be changed silently): dry-run default for every mutator;
writes refuse for unmeasured versions with **no override flag**; VST2 and unmeasured formats fail
closed; unconditional registration with typed `UnsupportedProvider`; user-local configurable cache
(`%LOCALAPPDATA%\reaper_reapy_mcp\cache\plugins`, `REAPER_MCP_PLUGIN_CACHE`).
Also out of scope: any change to the existing 57 tools or their error shape; new dependencies;
provider entry-point/discovery packaging; a second MCP server; an HTTP/IPC provider; harmonizing the
two error conventions; the POC's `vit` CLI workflows.

## 5. Source of proven behavior — and what must NOT be copied

**Reuse as behavior reference (read the POC; re-implement under V3 contracts):**
`bridge/parameters.py` (batched ident-addressed parameter IO), `bridge/state.py` (track-chunk
transport — *but note its first-block defect*), `bridge/locate.py` (identity fields), `adapter/*`
(registry, laws, capabilities, dependencies, modulations, wavetables), `adapter/transaction.py`
(snapshot/verify/rollback shape), `mcp/results.py` (envelope shape, superseded by schema 2), and the
POC's test suite as a behavior reference for V-1…V-12.

**Must not be copied or imported wholesale:**
- `_b4_lib.py` / `_bh_session.py` / `_b3_lib.py` — module-level `reapy`, `ReaperController`,
  `os.chdir(C:\repos\reaper-reapy-mcp\src)`, registry reads ([17 AV-05](17-planning-evidence-index.md)).
  Only the verified host-free function set ([17 AV-13](17-planning-evidence-index.md)) is ported.
- `bridge/state.py`'s targeting behaviour — it selects the **first** `<VST>` block, which is the
  AUD-001 defect; V3 replaces it with unique-or-refuse slice location.
- `capability_cache.py`'s write path — it overwrites whatever file is at the path (AUD-012); V3
  quarantines by atomic rename and writes temp+replace.
- The POC's L4 test harness — it stores a control vector but restores only document+routes (AUD-015);
  V3 compares **every** captured channel before the word "restored" may appear.

## 6. Mandatory boundaries

1. **`plugins/host/reaper.py` is the only module inside `plugins/` that imports `reapy`.** Core modules
   contain no provider identifiers (`vital`, `osc_`, `filter_1` — including comments).
2. **The provider never imports `reapy`, `ReaperController`, `_bh_session` or `_b4_lib`**, and importing
   it must not change the working directory. Enforced by a **recursive import-boundary test run in a
   fresh process** (V-12) — a build gate, not a warning.
3. **No provider callback inside the bridge**: the bridge owns transport (locate, splice, write,
   settle, re-read); the codec owns semantics (decode/encode/verify). AUD-004.
4. **No wall-clock sleeps in tests**; injected settle/retry timing only.
5. **No new dependency, no packaging change, no `pyproject.toml` edit** without a new owner decision.
6. **Never stage, revert, discard or commit unrelated work** — the staged
   `src/controllers/project_controller.py` change and the untracked `composer_v1_2_lm_probe.md` must
   survive every phase untouched. `git add -A`, `git commit -a` and `git checkout -- .` are banned;
   stage explicit paths only, and re-check `git status --porcelain -uall` before and after every commit.

## 7. Normative contracts (the documents that define behavior)

| Document | What it is normative for |
|---|---|
| [04-architecture.md](04-architecture.md) | boundaries, package topology, invariants I-1…I-17 |
| [05-universal-core-contract.md](05-universal-core-contract.md) | domain objects incl. §2.9 object set, protocols, identity model, error taxonomy |
| [06-mcp-tool-contracts.md](06-mcp-tool-contracts.md) | the 7 tools, arguments, envelope schema 2, dry-run defaults |
| [07-capability-profile-schema.md](07-capability-profile-schema.md) | profile schema, validation table, provider revision/artifact digest |
| [08-provider-and-host-interfaces.md](08-provider-and-host-interfaces.md) | Core/Provider/Codec/Host interfaces, provider loading, registry directory rules, import boundaries |
| [09-discovery-and-cache.md](09-discovery-and-cache.md) | cache key, quarantine, atomic write, fail-closed identity |
| [10-state-modulation-dependencies.md](10-state-modulation-dependencies.md) | targeting/slice contract, route model, depth, dependency rules |
| [11-planning-transactions-and-verification.md](11-planning-transactions-and-verification.md) | the safety core: stages, snapshot, apply, verify, rollback, verdicts, failure taxonomy, concurrency |
| [12-testing-strategy.md](12-testing-strategy.md) | test layers, mocks, case matrix B-1…B-56, evidence conventions |
| [13-existing-mcp-integration.md](13-existing-mcp-integration.md) | integration point, compatibility, rollout |
| [14-implementation-plan.md](14-implementation-plan.md) | P0–P10 with scope, work items, tests, acceptance, failure conditions, exit criteria, gates |
| [15-risks-and-open-questions.md](15-risks-and-open-questions.md) | risk register with triggers/owners, question statuses |
| [17-planning-evidence-index.md](17-planning-evidence-index.md) | every significant claim → evidence (source path/symbol or explicit design) |
| [18-remediation-register.md](18-remediation-register.md) | AUD-001…AUD-015 + DS-01…DS-12 dispositions and what remains to be implemented |
| [19-owner-decisions.md](19-owner-decisions.md) | the four approved decisions and the eight technical limitations |

**Conflict rule:** if code and document disagree, the document wins until the owner approves a change.
Contract changes are **documentation-first** ([00 §7](00-project-charter.md)): amend the document, then
implement.

## 8. Phase order, gates and what "passing" means

P0 contracts and approved decisions → P1 models, mocks and identity objects → P2 mock transaction
mechanics → P3 target-aware read bridge (L3 read validation) → P4 profile and cache → P5 host write
primitives and codec contract (no live mutation) → P6 mock modulation and dependency validation →
P7 Vital codec isolation and provider → P8 live mutation validation (first live writes) → P9 existing
MCP integration → P10 final acceptance.

Per-phase detail (A–K) and the ten global gates are in [14](14-implementation-plan.md#global-phase-gates).
The gates that matter most:

- **G-1/G-2**: no live mutation before target-aware addressing + identity exist **and** complete
  snapshot/recovery passes deterministic mock tests.
- **G-3**: no Vital live mutation before codec isolation and provider conformance pass.
- **G-4/G-5/G-6**: no write to a stale or ambiguous target; no rollback claim without per-channel
  comparison; no blind rollback over a detected external change.
- **G-7**: no final acceptance while post-write outcomes remain ambiguous.
- **G-8**: no phase depends on later work. **G-9**: no bypass of version restrictions.
- **G-10**: no silent scope expansion.

**A phase passes only when** its acceptance criteria are met with evidence artifacts, its failure
conditions did not occur, its exit criteria are satisfied, the full offline suite is green with counts
reported, and no unrelated file changed. A phase that cannot name its failure conditions has not been
planned well enough to start.

## 9. Required tests (specified, none run yet)

| Layer | What it covers | Where |
|---|---|---|
| L0 boundary | `plugins/` import boundaries, provider cleanliness, no provider identifiers in core | [12 §2](12-testing-strategy.md) |
| L1 unit/contract | models, identity, revisions, error serialization, profile validation, cache key | [12 §3](12-testing-strategy.md) |
| L2 mock | transaction state machine, verdict matrix B-45/B-46, recovery B-32…B-37, concurrency B-50…B-54, cache B-47/B-55/B-56, targeting B-26…B-31 | [12 §5](12-testing-strategy.md) |
| L3 live read | real instance enumeration/identity/parameter batch/slice read; FX-GUID measurement; counters | [12 §4](12-testing-strategy.md) |
| L4 live write | full-channel capture → write → verify → restore → verify restore, per-channel, on the designated test project only | [12 §7](12-testing-strategy.md) |
| L5 provider | V-1…V-12 provider conformance and regressions | [14 P7](14-implementation-plan.md) |

Rules: no test uses a wall-clock sleep; every live result is an evidence artifact (per-run name,
content hash, never overwriting another run); a mock must expose every host failure mode it fakes; and
**no behavior is claimed without a test that has actually run**.

## 10. When the Implementer must stop and ask

Stop the phase, report, and request a decision when any of these happens:

1. An approved decision (1–4) would have to be contradicted to make progress.
2. A normative contract needs to change ([05]/[06]/[07]/[08]/[09]/[11]) rather than be implemented.
3. A new dependency, packaging change or new tool/tool argument is required (G-10).
4. A measurement makes an approved guarantee impossible (e.g. complete capture cannot be obtained on
   the live host, or the measured capture cost is prohibitive → the narrower write surface would be a
   new decision).
5. A gate cannot be satisfied as written (G-1…G-10).
6. Unrelated user work is at risk (staged change, untracked files) or git state is not what the phase
   expects.
7. A live test would touch anything other than the designated test project, or a precondition of the
   live harness fails.
8. The evidence contradicts a document (the document wins until amended), or an outcome stays
   `unknown`/`partially_restored` and cannot be explained.

## 11. Runtime behaviors that are NOT yet verified

Everything below is a specification or a planned measurement — **never report it as working**:

| # | Unverified today | How/when it becomes verified |
|---|---|---|
| L-1 | External-race detection is best-effort; no compare-and-swap exists in the host API | B-50…B-54 on mocks (P2), live conflict behaviour (P8) |
| L-2 | The process cannot start without REAPER; nothing is returned at invocation level in that case | documented behavior (P9 evidence), not a test of an impossible case |
| L-3 | Identical-build plugin replacement may be indistinguishable to the host | `TrackFX_GetFXGUID` measurement (P3), fail-closed policy |
| L-4 | Full parameter-vector capture cost is **not measured** (feasibility only) | measured at P5 (mock/captured corpus) and P8 (live) |
| L-5 | Only measured versions/formats supported (Vital 1.5.5 VST3); VST2 fails closed | version/format gate tests (P7), acceptance (P10) |
| L-6 | Modulation/derived values may not be attributable to a stored parameter value | codec authority declarations + [10] semantics |
| L-7 | Splice byte-fidelity is proven only on captured chunks until a live multi-block write is measured | P5 fixtures + P8 multi-block live case |
| L-8 | A transport failure between multiple host calls is observable but not preventable | per-unit records + truthful outcomes |
| — | **All of AUD-001…AUD-015 corrections are documentation-only** (`CORRECTED_IN_DOCS`) | each becomes a claim when its test passes in its phase |

## 12. Future implementation-chat instructions (copy into the implementation session)

> **Target:** `C:\repos\reaper-reapy-mcp`. **Start at P0** and follow
> `docs/universal-plugin-mcp-v3/14-implementation-plan.md` in order.
>
> 1. Begin with P0 and implement **one bounded subsystem at a time**; do not batch phases.
> 2. Write and run the phase's tests before moving on; report suite counts honestly.
> 3. Preserve unrelated user changes (staged `project_controller.py`, untracked `composer_v1_2_lm_probe.md`);
>    never stage, unstage, revert or discard them; never use `git add -A` or `git commit -a`.
> 4. Respect every gate in [14 §Global phase gates](14-implementation-plan.md#global-phase-gates);
>    in particular, no live mutation before P8 and none at all until P3/P2/P7 have passed.
> 5. Update documentation with **verified** behavior only; a correction becomes a claim when its test
>    passes. Never mark anything `VERIFIED` because it is described.
> 6. Stop and ask the owner when §10 above applies.
> 7. Never claim untested runtime behavior works.
> 8. Never modify the approved architecture silently: contract changes are documentation-first, and an
>    approved decision ([19](19-owner-decisions.md)) is only changed by the owner.

## 13. Handoff checklist for the first implementation session

- [ ] Read this document, then [04](04-architecture.md), [05](05-universal-core-contract.md),
      [06](06-mcp-tool-contracts.md), [08](08-provider-and-host-interfaces.md),
      [11](11-planning-transactions-and-verification.md), [12](12-testing-strategy.md),
      [14](14-implementation-plan.md), [19](19-owner-decisions.md).
- [ ] Confirm the repository state matches §2 and that no unrelated change is staged.
- [ ] Confirm the four decisions are `OWNER_APPROVED` in [19 §6](19-owner-decisions.md) and that the
      package received its final sign-off.
- [ ] Confirm no runtime source file has been created yet (`src/reaper_reapy_mcp/plugins/` absent).
- [ ] Create the P0 package skeleton **only**, run the P0 tests, and report before starting P1.
