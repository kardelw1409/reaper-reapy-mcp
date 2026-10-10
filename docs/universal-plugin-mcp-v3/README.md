# Universal Plugin MCP V3 — Planning Documentation

**Status: PHASE D — SPECIFICATION CONSOLIDATED, AWAITING OWNER APPROVAL.**
The independent GPT-5.6 audit (Phase B) returned NOT READY with 3 Critical / 8 High / 4 Medium
findings; all fifteen were verified against source and corrected (dispositions:
[18-remediation-register.md](18-remediation-register.md)). Phase D consolidated the specification:
four genuine owner decisions are collected in [19-owner-decisions.md](19-owner-decisions.md); every
other open question now has a source-resolved answer, a recommended default or a V1 deferral
([15 §2](15-risks-and-open-questions.md)). **IMPLEMENTATION IS NOT APPROVED.** No runtime module,
MCP tool, provider port or migration described here may be implemented before the owner's explicit
approval recorded in [19 §6](19-owner-decisions.md). Nothing in this set has been implemented or
executed.

- Date of this revision: 2026-10-11 (Phase C remediation, Phase D consolidation)
- Target repository: `C:\repos\reaper-reapy-mcp` (this repository)
- POC / evidence repository: `C:\REAPER` (REAPER portable install; Vital POC under
  `C:\REAPER\Scripts\sound_design\vital`, data under `C:\REAPER\prompts\vital_sound_design`)

## What V3 is

A **plugin-aware semantic layer** that will be integrated into the existing REAPER MCP server —
not a second MCP server. It gives an LLM/agent a stable, provider-agnostic way to describe, read
and write a *specific plugin instance* (first provider: Vital 1.5.5 VST3 inside REAPER), with
preflight, planning, transactional writes, verified read-back and rollback.

`LLM / Agent → existing REAPER MCP (host-level tools) → Universal Plugin Tools → Provider (Vital) → REAPER Host Bridge → VST3 plugin instance`

The **existing REAPER MCP keeps ownership of host-level concerns** (tracks, FX chain, items, MIDI,
transport, projects, rendering). V3 adds a plugin-instance layer; it does not duplicate host tools.

## Reading order

| # | Document | Purpose |
|---|---|---|
| — | this file | Objective, status, boundaries, reading order |
| 00 | [Project charter](00-project-charter.md) | Problem, goals, non-goals, constraints, gates |
| 01 | [Existing system audit](01-existing-system-audit.md) | What the REAPER MCP repo actually is today |
| 02 | [Vital POC audit](02-vital-poc-audit.md) | What the working POC actually implements |
| 03 | [Component responsibility matrix](03-component-responsibility-matrix.md) | Per-component classification + reuse verdicts |
| 04 | [Architecture](04-architecture.md) | Boundaries, flows, invariants, diagrams |
| 05 | [Universal Core contract](05-universal-core-contract.md) | Domain objects, interfaces, errors, invariants |
| 06 | [MCP tool contracts](06-mcp-tool-contracts.md) | The proposed 7-tool surface, schemas, examples |
| 07 | [Capability profile schema](07-capability-profile-schema.md) | The versioned profile format + validation |
| 08 | [Provider and host interfaces](08-provider-and-host-interfaces.md) | Core/Provider/Codec/Host boundaries |
| 09 | [Discovery and cache](09-discovery-and-cache.md) | Lifecycle, cache key, invalidation, refusals |
| 10 | [State, modulation, dependencies](10-state-modulation-dependencies.md) | Codec contract, route model, rule engine |
| 11 | [Planning, transactions, verification](11-planning-transactions-and-verification.md) | The safety core: plan → apply → verify → commit/rollback |
| 12 | [Testing strategy](12-testing-strategy.md) | Layers, mocks, boundary cases, acceptance criteria |
| 13 | [Existing MCP integration](13-existing-mcp-integration.md) | Where and how V3 plugs in; compatibility |
| 14 | [Implementation plan](14-implementation-plan.md) | Dependency-ordered phases P0–P10 |
| 15 | [Risks and open questions](15-risks-and-open-questions.md) | Risk register + unresolved questions |
| 16 | [Independent review checklist](16-independent-review-checklist.md) | For the GPT-5.6 Codex audit (Phase B) |
| 17 | [Planning evidence index](17-planning-evidence-index.md) | Every significant claim → evidence |
| 18 | [**Remediation register** (Phase C)](18-remediation-register.md) | AUD-001…AUD-015 dispositions → corrected contracts → tests |
| 19 | [**Owner decisions** (Phase D approval)](19-owner-decisions.md) | The four decisions that need the owner; everything else adopted as recommended defaults |

## Evidence discipline

Every important claim in these documents carries one of four tags (defined by the project brief, §22):

- **[A] Verified in source** — actual code was read; path and symbol are cited.
- **[B] Verified by tests** — a specific test exists and its meaning is recorded. *Existence of a
  test file is not success evidence; no test suite was executed during Phase A.*
- **[C] Observed runtime / forensic evidence** — a recorded run, log, or artifact; provenance recorded.
- **[D] Proposed / inferred / unresolved** — design proposals and unverified inferences, labelled as such.

Claims sourced from subagent research (GLM-5.3 Flash research tasks, see §"Research assistance"
below) were cross-checked against source before being written here; anything not cross-checked is
marked **[D]**.

## Explicit statements

1. **Implementation has not been approved.** Nothing in this documentation authorizes code changes.
2. There is **no second REAPER MCP**; V3 is integrated into `reaper_reapy_mcp`.
3. The Vital Adapter source is **not copied** into this repository by this plan. Per the Phase-D
   recommendation ([19 Decision 2](19-owner-decisions.md)), the Vital provider is an **in-repo,
   dependency-isolated provider package** (`plugins/providers/vital/`) whose codec is the extracted
   host-free byte algorithm (verified in source: [17 AV-13](17-planning-evidence-index.md)), and the
   legacy `_b4_lib`/`_bh_session` import chain is never imported (AUD-005). Registry data is read
   from a configured directory, not vendored by default. Owner approval of Decision 2 is pending.
4. The POC's proven behavior is preserved **only after** its dependencies and boundaries were
   examined — see [03](03-component-responsibility-matrix.md); corrected claims are listed in
   [18](18-remediation-register.md) (state targeting, rollback scope, cache save, L4 evidence).
5. The existing REAPER MCP has a pre-existing staged uncommitted change
   (`src/controllers/project_controller.py`, 26 insertions / 3 deletions, staged) and an untracked
   probe document; this planning phase did not touch either. See [01 §7](01-existing-system-audit.md).
6. **Honest-guarantee statements (Phase C):** success = verified read-back; a rollback is "verified"
   only per captured channel; "zero host writes" is claimed only at `write_calls_attempted == 0`;
   the host offers no compare-and-swap, so revision checks are best-effort conflict detection and
   the residual external-race window is documented, not denied ([11 §7](11-planning-transactions-and-verification.md)).
7. Every mutating tool defaults to **dry-run**; application requires an explicit `dry_run: false`
   (AUD-009).

## Research assistance (disclosure)

Phase A used the environment's GLM-5.3 Flash research subagent for **bounded, read-only inventory
tasks** (POC MCP surface, bridge API map, test inventory, transaction semantics, model/modulation
inventory, target-repo tool inventory, knowledge-base role, POC architecture-document digest).
All architecture-critical claims (write order, verification, cache key, identity, registration,
git state, launch config) were re-verified directly against source by the primary author. Subagent
reports were used as accelerators, not as decision-makers; where a report and source disagreed,
source won and the disagreement is recorded (e.g. the POC README's "12 tools" vs the actual 24 in
`mcp/tools.py`, see [02 §4](02-vital-poc-audit.md)).

The brief named a "GLM 5.3 Flash subagent" as *optional*; it was available and used. It produced no
architecture decisions, no canonical documentation, and no approvals.
