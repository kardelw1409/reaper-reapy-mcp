# 16 — Independent Review Checklist (GPT-5.6 in Codex)

Purpose: a practical, verifiable checklist for the audit. This revision is the **re-audit
checklist for the Phase-C-remediated specification** (round 1 produced AUD-001…AUD-015; dispositions
and corrections are registered in [18](18-remediation-register.md)). Each item states **what to
check**, **where to look**, and **what a failure would mean**. Verify claims against source, not
against these documents' summaries.

## 0. How to use this document

1. Read [README](README.md) → [00](00-project-charter.md) → [04](04-architecture.md) → [18](18-remediation-register.md)
   (dispositions) first.
2. Then audit in the order below; every claim carries an evidence tag ([A]-[D], brief §22).
3. Any claim you cannot support from source is a finding — including claims labelled [A] here.
4. For each previously raised finding, check the corrected contract in the named document AND that
   dependent documents were synchronized (the audit's own §20 cross-document requirement).
5. Record findings with: severity, affected document, evidence, and whether the document must change
   or the code does (there is no code yet; the audit examines documents + POC/target source).

## 1. Source audit — is the existing system described accurately?

- [ ] Does `src/reaper_reapy_mcp/server.py` really construct `ReaperController` → `FastMCP("Reaper Control")`
      → `setup_mcp_tools` → `main()` runs `.run()`? ([01 §2])
- [ ] Are there really **57** `@mcp.tool` registrations in `src/mcp_tools.py`, and are the grouped
      lists in [01 §5] accurate (spot-check 5 names per group, plus total count)?
- [ ] Is the response pattern really `{"status": ...}` + catch-all `except` in both controllers and
      wrappers? ([01 §5]) Spot-check 3 tools.
- [ ] Does `BaseController.__init__` call `reapy.connect()` eagerly, and does `verify_connection()`
      really always return `True`? ([01 §3])
- [ ] Is the `add_fx` defect real: `mcp_tools.py` compares a bool against `>= 0`? ([01 §4])
- [ ] Is it true that **no** chunk/ident/plugin-state logic exists anywhere in `src/`? (grep
      `GetTrackStateChunk`, `GetParamIdent`, `Vital`)
- [ ] Is the staging state real and untouched: `git status --porcelain -uall` shows
      `M  src/controllers/project_controller.py` and `?? composer_v1_2_lm_probe.md`; `git diff`
      (unstaged) is empty? ([01 §7])
- [ ] Does `C:\REAPER\.vscode\mcp.json` register only `reaper` (plus non-plugin servers) with the
      exact launch string quoted in [01 §2]? Is the user-level `mcp.json` empty?

## 2. POC audit — are the mechanics described faithfully?

- [ ] **Transaction:** does `adapter/transaction.py` implement snapshot → validate →
      document write → parameter batch → verify → commit, with rollback on failure; is the order
      enforced (reversed order refused by test); is rollback verified **for the channels it captured**
      (document+routes only); are parameters really NOT restored and reported as a named gap?
      ([02 §12], [11]) — **and confirm the corrected scope statement:** the POC's rollback is not a
      full-session rollback, and a document restore can reset parameters (AUD-002; [02 §12 item 6]).
- [ ] **State targeting (AUD-001):** confirm the documented limitation against source —
      `_b4_lib.find_vst_block` selects the FIRST `<VST>` block; `bridge/state.py::read_document`
      passes the full chunk to `parse_framing` while its comment claims a single-block view; the
      `select_block` result is reporting-only; `rebuild_chunk` splices around the first block.
      Check that no V3 document still claims multi-VST targeting is proven. ([02 §10], [10 §1.2],
      [17 AV-01…AV-04])
- [ ] **Legacy codec import chain (AUD-005):** confirm `_b4_lib` → `_bh_session` pulls in `reapy`,
      `ReaperController`, `os.chdir(...\src)` at module level. ([02 §15], [17 AV-05])
- [ ] **Success criterion:** is there a line/docstring asserting success = verified read-back, never
      the API return (`SetTrackStateChunk` untrusted)? ([02 §12], [11 §2.5])
- [ ] **Verification tolerances:** state `1e-5`, parameter `1e-3`, executor restore `1e-6`
      both-channel — confirm each symbol/line. ([02 §12], [11 §2.5])
- [ ] **Write order reason:** is the "document write resets parameters" measurement recorded
      (cutoff 0.20→0.54688)? ([02 §12])
- [ ] **One route per state write:** is the limit implemented (sequential single-route writes,
      re-read between) and is the measurement evidence named? ([02 §12.3], [10 §1.2, rule 6])
- [ ] **Capability cache:** key = identity (with required `vst_class`+`param_count`) + registry
      digests + format key; refusal cases; truncation fails two checks; storage under
      `logs/capability_cache/`. **And confirm the corrected claim (AUD-012):** `save()` overwrites
      whatever sits at the path without identity validation and writes non-atomically — the V3
      quarantine/atomic-write rules are new requirements, not inherited behaviour. ([02 §7], [09 §5],
      [17 AV-06/AV-07])
- [ ] **Identity:** GUID read but never used as identity; ambiguity raises; fingerprint re-checked
      with one re-resolve; version gate `MEASURED_VERSIONS=("1.5.5",)`. ([02 §5])
- [ ] **Modulation:** slot identity; all route fields written via the document; depth conversion is
      per-destination fitted with recorded refusals; `LINE_MAPPING_WRITE_SUPPORTED=False`. ([02 §9])
- [ ] **Dependencies:** findings with kinds; `raise_on` before any write; DISABLE-set as a
      transition. ([02 §11])
- [ ] **Bridge:** single `bindings.call` chokepoint with timeout/wedge; batch IO inside REAPER;
      ident resolved inside the hop; dialog watchdog; `state.py` mixed (transport + Vital codec).
      ([02 §13], [03 verdicts])
- [ ] **POC tool surface count:** 24 names in `TOOL_NAMES` (the README's "12 tools" is stale).
      ([02 §3])
- [ ] **L4 evidence scope (AUD-015):** confirm `test_l4_live_write.py` stores a control vector but
      its `restore()` applies document+routes only and verifies the routes predicate — the vector is
      never re-applied or compared. ([02 §16], [17 AV-08])
- [ ] **Absence of token/revision machinery (AUD-003):** confirm no write-target-token or
      state-revision concept exists in the POC. ([02 §5], [17 AV-09])
- [ ] **POC own decisions:** DEC-01 (separate server, two recorded reasons), DEC-22 (success =
      verified read-back), DEC-19 (named gaps), DEC-11 (never GUI/pointer identity), DEC-25
      (line_mapping), DEC-23 (a wrong claim corrected by measurement). ([02 §17])

## 3. Boundaries — are Core/Provider/Codec/Host separated correctly?

- [ ] Does [04 §1] place *semantic mechanics* in Core, *plugin semantics* in Provider, *document
      format* in the codec, and *transport* in the host bridge? Is any Vital fact smuggled into Core
      anywhere in [05]-[11]? (Grep the docs for Vital-specific names outside provider-mapping
      sections.)
- [ ] Is the **targeting boundary** right: bridge locates+splices the slice (transport), codec
      decodes/encodes/verifies it (semantics), Core never sees block bytes? ([05 §2.9/§3], [08 §3-§4],
      [10 §1.1])
- [ ] Is the **identity model** three separate concepts (profile fingerprint × target token ×
      state revision), with the token never derived from a mutable state digest and never
      serialized as durable identity? ([05 §5])
- [ ] Is the "never read a control value from the document" rule generalized correctly into
      *declared read/write authority per field* rather than copied as a universal rule? ([05 §4],
      [04 I-5])
- [ ] Is `write_order` treated as provider-declared but **not** caller-configurable, and is
      restoration order similarly provider-declared ([11 §2.3a])?
- [ ] Is the codec split from the host transport justified against `bridge/state.py`'s actual
      content — including the AUD-001 targeting defect? ([03 matrix rows, 10 §1])
- [ ] Is the **provider placement decision** ([08 §6](08-provider-and-host-interfaces.md),
      [19 Decision 2](19-owner-decisions.md)) consistent with the brief, and are the extraction
      boundary ([17 AV-13](17-planning-evidence-index.md)) and the alternatives honestly documented
      (AUD-005)?
- [ ] Does anything in [06] require the LLM to know indices/idents/document paths? (It must not.)
- [ ] Is the package topology single and consistent everywhere: `plugins/host/{contract,reaper}.py`,
      core modules flat, boundary tests scoped to `plugins/` only (AUD-013)? ([04 §6], [08 §8], [12 §2])

## 4. Contracts — are they complete and implementable?

- [ ] [05 §2] field tables: every object used elsewhere in the docs is defined once, with required
      fields; no object is referenced but undefined. **Check the §2.9 object set explicitly**
      (`FxHostFacts`, `TargetedStateSlice`, `StateRevision`, `StateSnapshot`, `StateWriteUnit`,
      `StateWritePlan`, `WriteAttempt`, `VerificationReport`) — each has owner + invariants + tests,
      and no retained object lacks a consumer (AUD-004).
- [ ] [06]: all 7 tools have input/output/errors/side effects/verification/tests/examples; the
      **envelope schema 2** separates `error` from `outcome`; the host accounting fields distinguish
      attempted / verified / unknown write effects (AUD-007); **every mutator defaults to
      `dry_run=true`** (AUD-009) and the tool table classifies every op (read-only / dry-run /
      mutating / filesystem-mutating).
- [ ] [07]: profile schema validation table has a test per row; **the cache key includes
      `provider_revision` + artifact digest** (AUD-008); fail-closed identity rules present;
      version policy is refuse-no-override (AUD-014 Q-03).
- [ ] [09]: cache key composition normative; the **corrected five refusals** (quarantine + atomic
      write) each with a test; a foreign file's bytes provably preserved (AUD-012).
- [ ] [11]: every verdict (8, incl. `NO_WRITES`) defined with its `final_state` mapping; failure
      taxonomy F1–F12 complete; **snapshot-or-refuse** normative (AUD-002); restoration order
      provider-declared and per-channel-verified; atomicity claims qualified ("transactional with
      verified compensation", never "atomic") and the residual external-race limitation explicit
      (AUD-010).
- [ ] Do the docs contradict each other? (Cross-check: tool list vs [13 §3]; verdict/`final_state`
      names between [11 §3] and [05 §2.8]; capture scope between [06 §7], [07 §2/§3], [11 §2.3];
      cache rules between [07 §5] and [09 §5]; phase IDs between [13 §7] and [14]; startup claims
      between [06 §10], [13 §4/§6] and [01 §3]; tool count 7 vs registration notes.)

## 5. Safety

- [ ] `plugin_write` refusals carry `write_calls_attempted == 0` and dry-run (the default) writes
      nothing; the plan is data. ([06 §7], [11 §2.1-§2.2, §4])
- [ ] **Snapshot-or-refuse (AUD-002):** the transaction refuses before the first write when the
      capture of any mutable channel (document, routes, parameter vector) fails; `not_captured`
      inside the capture scope is a refusal for writes. ([11 §2.3], [07 §2])
- [ ] **Rollback per channel:** `FAILED_AND_RESTORED` is impossible unless every captured channel
      was verified after restoration; partial/unknown/failed outcomes are distinguished and loud;
      an attempted rollback is never equated with a verified one. ([11 §2.7, §3])
- [ ] **No silent re-target:** token staleness ⇒ `TargetChanged`; ambiguous slice correlation ⇒
      `TargetSliceAmbiguous`; both zero-write refusals. ([05 §5], [10 §1.2], [11 §4])
- [ ] **Revision honesty:** expected revision advances from the transaction's own verified writes;
      external changes ⇒ `StateConflict`; the no-compare-and-swap residual risk is documented and
      marked for owner acceptance (Q-12). ([11 §6-§7])
- [ ] The space-correctness rule (compare in the written space) is normative, with the POC's
      recorded defect cited. ([11 §2.5])
- [ ] No claim of host-level atomicity anywhere; no claim that revision checks eliminate external
      races (AUD-010/19.4).
- [ ] Cache writes cannot destroy a foreign/mismatched file (quarantine) and are atomic/interrupt-
      safe (AUD-012). ([09 §4-§5])

## 6. Tests — is the strategy sufficient?

- [ ] [12] covers: unit, contract, boundary (**B-1…B-56**), mock (two codecs), live read, live write
      (full-channel self-restoring), Vital regression (**V-1…V-12**).
- [ ] Every boundary row has an expected outcome and a "how reached on mocks" implication;
      specifically: AUD-001 fixtures (B-26…B-31), recovery (B-32…B-37), identity/revision
      (B-38…B-43), outcome schema (B-45/B-46), cache key/quarantine (B-47/B-55/B-56), mutation
      policy (B-48/B-49), concurrency/cancellation (B-50…B-54).
- [ ] Failure conditions list ([12 §10]) genuinely gates phases (wrong-space verification, undeclared
      restore claims, blocks not proven byte-identical, defaults that apply, missing revalidation,
      counters that overstate, foreign-file destruction, key omitting provider logic, legacy import).
- [ ] Are the live-write safety rules adequate (designated test project, identity precondition,
      **per-channel capture/restore/compare**, untouched-block byte-proof, whole-range digest,
      artifact naming), and is the live-write gate placed after P7 (AUD-011)?
- [ ] Do test obligations map 1:1 to phase exit criteria in [14]? (Spot-check P2, P5, P7, P8, P9.)

## 7. Implementation readiness

- [ ] Is the phase order dependency-correct per AUD-011? (P3 target-aware read bridge before any
      write primitive; P5 write primitives before P6/P7; P7 provider before P8 live mutation; P9
      registration; P10 acceptance; early mock registration tests at P2/P6.)
- [ ] Does each phase have: goal, prerequisites, files, contracts, tests, acceptance criteria, risks,
      verification, exit criteria? ([14])
- [ ] Is the **remediation register ([18])** consistent: every AUD-001…AUD-015 has a status, the
      impacted documents match the actual edits, and no finding is marked `VERIFIED` on the basis of
      documentation alone?
- [ ] Are the risks in [15] owned and triggered? Does every open question (Q-01…Q-13) carry a
      status, with the four `OWNER_APPROVAL_REQUIRED` items pointing at
      [19-owner-decisions.md](19-owner-decisions.md) and no item marked `OWNER_APPROVED` without the
      owner? Is Q-12 (residual external race) explicit about what cannot be excluded?
- [ ] Is [19](19-owner-decisions.md) consistent with every document it cites — and is it genuinely
      limited to decisions that change scope, existing behaviour or accepted risk (nothing routine
      escalated, nothing decided quietly)?
- [ ] Do all documents state the **same** provider placement, codec-extraction approach, recovery
      guarantee and startup lifecycle — with no stale "out-of-tree" or "proposed-only" wording left
      anywhere ([19](19-owner-decisions.md) is the single source for those four positions)?
- [ ] Is the evidence index [17] usable: every significant design decision traceable to a source
      path/symbol or explicitly [D], including the Phase-C verification rows (AV-01…AV-12)?
- [ ] Is there anything that **requires** implementation knowledge not yet obtained? (If yes, it is
      a finding against documentation completeness. Known open measurements: FX-GUID replacement
      behaviour (P3), parameter-capture cost (P5/P8).)
- [ ] Confirm the mandatory statement: implementation not approved; no code changes were made during
      Phases A-D. (`git status --porcelain -uall` in the target repo shows only the pre-existing
      staged `project_controller.py` change, the untracked probe doc, and the untracked
      `docs/universal-plugin-mcp-v3/`; nothing staged by the planner.)

## 8. Suggested audit order (highest risk first)

1. [19](19-owner-decisions.md) — the four approval decisions and the adopted defaults; confirm every other document is consistent with them before auditing content.
2. [18](18-remediation-register.md) + [17 §J](17-planning-evidence-index.md) — the dispositions and their source evidence, so the re-audit tests the corrections rather than re-derives round 1.
3. [11](11-planning-transactions-and-verification.md) + [02 §12] + `adapter/transaction.py` — the safety core (capture, order, per-channel rollback, outcome, concurrency).
4. [10 §1](10-state-modulation-dependencies.md) + `_b4_lib.py`/`bridge/state.py` — the targeting defect and the slice contract (AUD-001).
5. [09](09-discovery-and-cache.md)/[07](07-capability-profile-schema.md) + `capability_cache.py` — key determinants and cache-write safety (AUD-008/AUD-012).
6. [08 §6](08-provider-and-host-interfaces.md) + `_bh_session.py` imports — isolation (AUD-005) and the extraction boundary ([17 AV-13](17-planning-evidence-index.md)).
7. [05 §5](05-universal-core-contract.md)/[06](06-mcp-tool-contracts.md) — identity model + envelope + mutator defaults (AUD-003/007/009).
8. [13](13-existing-mcp-integration.md) + [01 §3] — lifecycle Option A and unconditional registration (AUD-006/AUD-013).
9. [12](12-testing-strategy.md) + [14](14-implementation-plan.md) — readiness, phase order (AUD-011), per-phase failure conditions and the mandatory gates.
10. [03](03-component-responsibility-matrix.md), [15](15-risks-and-open-questions.md), remaining cross-checks.
