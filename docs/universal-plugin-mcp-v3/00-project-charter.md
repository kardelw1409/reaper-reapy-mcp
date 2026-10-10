# 00 — Project Charter

Status: **[D] proposed** (rev. 2026-10-10, Phase C remediation → Phase D consolidation → Phase E approvals and handoff — see [18](18-remediation-register.md), [19](19-owner-decisions.md), [20](20-implementation-handoff.md);
Phase-B audit findings AUD-001…AUD-015 resolved in documentation).

## 1. Problem

The existing REAPER MCP (`C:\repos\reaper-reapy-mcp`) controls REAPER at host level: tracks, FX
chain placement, items, MIDI, transport, rendering. It has **no plugin-instance layer**: tools such
as `set_fx_param` address parameters by name substring or numeric index, values are untyped
normalized floats, there is no capability metadata, no state/document channel, no transaction, no
verification and no rollback ([01](01-existing-system-audit.md) [A]).

A working POC inside `C:\REAPER` (the "Vital Adapter") already demonstrates the missing layer for
one plugin — Vital 1.5.5 VST3 — including semantic controls, a measured capability registry,
modulation routes, a state/document channel, dependency rules, planning, verified transactional
writes, a rollback verified for the channels it captured (document+routes; parameters are a named
gap — AUD-002), and a capability cache ([02](02-vital-poc-audit.md) [A][C]).

That POC was deliberately built as a **separate MCP server** (`vital`) because the existing REAPER
MCP's controllers swallow exceptions and have no batch primitive — a decision recorded as DEC-01 in
the POC's own `DECISIONS.md`, with the rationale written into `vital/mcp/server.py`'s docstring [A].
V3 revisits that decision **conditionally**: the plugin layer can be integrated into the existing
server *if and only if* it is not forced through the swallowing controller semantics and not forced
into a single-parameter tool shape.

## 2. Goals

1. **One server.** The Universal Plugin layer becomes part of the existing `reaper_reapy_mcp`
   FastMCP server; no competing REAPER MCP server is created.
2. **Semantic plugin interface.** The LLM works with semantic control identities, typed values,
   module topology, modulation routes and declared dependencies — never raw VST parameter indices.
3. **Provable writes.** Every mutation is preflighted, planned, applied through a transaction,
   verified by read-back on both channels, committed only on verified state, and rolled back with a
   rollback verified **per captured channel** otherwise. "Host API returned true" is never success.
4. **Provider separation.** Plugin-specific knowledge (Vital today; other plugins later) lives in a
   Provider implementing a narrow contract. The Universal Core contains no Vital assumption.
5. **Host separation.** REAPER-specific transport (track/FX resolution, parameter calls, track state
   chunks) lives behind a Host Bridge. Core never imports `reapy`.
6. **Cache without lies.** Capability profiles are cacheable per (identity + registry digest +
   format); a stale or foreign cache entry is refused, never served ([09](09-discovery-and-cache.md)).
7. **Reuse proven behavior.** The V3 mechanics are derived from the POC's measured behavior, its
   recorded defects and its invariants — not re-invented ([03](03-component-responsibility-matrix.md)).
8. **Testable by construction.** Contract tests, boundary tests, mock-host tests and a self-restoring
   live-write harness are specified before implementation ([12](12-testing-strategy.md)).

## 3. Non-goals (explicitly out of scope)

- A universal sound-design engine, sonic vocabulary or taste decisions (LLM/agent responsibility).
- Preset marketplace / plugin downloader / render-audition subsystem.
- A "plugin framework ecosystem": exactly **one** concrete provider (Vital) for V1.
- A complex dependency DSL: rules are provider-authored data + a small core evaluator.
- Automatic plugin/module classification; global synth databases.
- A second independent REAPER MCP server; duplication of project/items/MIDI/transport tools.
- Copying or migrating the Vital Adapter source into this repository.
- Rewriting the existing controllers (their defects are recorded, not fixed, by this plan).
- **Persistent restore points** (arbitrary-path snapshot/restore artifacts) — deferred from V1
  (AUD-009 / Q-09).
- **A raw/parameter-index escape hatch** in the V1 tool surface (AUD-014 / Q-08).
- **VST2 Vital support** — fails closed with a typed refusal (Q-11).
- **Lazy-startup redesign of the server** — AUD-006 Option B; requires explicit owner approval and is
  not part of V1.

## 4. Scope of V1

| In scope | Out of scope |
|---|---|
| Host: REAPER via reapy, VST3 plugin instances on tracks | AU / CLAP; non-Windows hosts |
| Provider: Vital 1.5.5 VST3, formed as an **in-repo, dependency-isolated provider package** (`plugins/providers/vital/`) reading the POC registry data from a configured directory ([19 Decision 2](19-owner-decisions.md)) | Porting the POC's Vital parser/codec code into the repo; importing the legacy `_b4_lib`/`_bh_session` chain |
| Controls, modules, typed values, dependencies, routes, state/document **inspect**, transactional snapshot/restore (internal, in-memory) | Host automation envelopes as a plugin concept (REAPER owns automation); persistent restore-point files |
| 7 MCP tools (see [06](06-mcp-tool-contracts.md)) plugged into the existing server | New host-level tools; changes to the existing 57 tools |
| Mock-provider + mock-host test layers; live read tests; explicit live-write harness | Unattended/live write tests in default test runs |

## 5. Constraints (from evidence)

1. **Existing server semantics.** `ReaperController` methods return `bool`/`[]`/`None` and swallow
   exceptions; the MCP wrappers convert falsy returns into `{"status":"error"}` strings. Plugin
   tools must not consume these methods ([01 §4](01-existing-system-audit.md) [A]).
2. **Existing startup (AUD-006 Option A).** `BaseController.__init__` calls `reapy.connect()`
   eagerly; the process cannot start without REAPER unless that changes. V1 **preserves** this
   pre-existing lifecycle and documents it — the plugin layer adds no eager connection of its own and
   makes no promise about a server that never started; a connection lost *after* startup yields a
   typed `NotConnected` ([01 §3](01-existing-system-audit.md) [A], [13 §4/§6](13-existing-mcp-integration.md)).
3. **Launch configuration is fixed by the client.** The server is launched by
   `C:\REAPER\.vscode\mcp.json` as `py -3.11 -c "...sys.path.insert(0, r'C:\repos\reaper-reapy-mcp\src'); from reaper_reapy_mcp import main; main()"` [A]. Any integration must work under that
   launch shape (no packaging requirement).
4. **Measured host behavior** (POC): batch parameter IO must run inside REAPER (~680× cheaper than
   socket round-trips); a track-chunk state write resets ordinary parameter values to the document
   snapshot (so document-before-parameters ordering is mandatory for Vital); one state write lands
   at most one modulation route on 1.5.5; `SetTrackStateChunk` return value is not evidence
   ([02 §7-§9](02-vital-poc-audit.md) [A][C]).
5. **The POC registry is generated data with no runtime staleness check**; the cache must therefore
   key on a digest of the registry files it derived from ([02 §6](02-vital-poc-audit.md) [A]).
6. **Uncommitted user work exists** in the target repo (staged `project_controller.py` change,
   untracked probe markdown). Phases must be sequenced so they do not depend on, and never revert,
   that work ([01 §7](01-existing-system-audit.md) [A]).

## 6. Success criteria (V1)

1. `plugin_instances`, `plugin_describe`, `plugin_find`, `plugin_read`, `plugin_write`,
   `plugin_state`, `plugin_modulation` are registered **unconditionally** on the existing server and
   callable through its stdio transport alongside the 57 existing tools, with no name collisions
   (AUD-013).
2. **Mutating tools apply nothing by default:** `plugin_write`/`plugin_modulation` mutations require
   an explicit `dry_run: false`; a dry-run performs no host write and returns a plan with dependency
   findings (AUD-009).
3. A successful `plugin_write` is always backed by a read-back comparison on every written channel,
   reported per item; a failing write yields typed errors, a rollback **verified per captured
   channel**, and the full post-write outcome (`stage`/`verdict`/`final_state`/`rollback`/counters) —
   never a success payload, and never `FAILED_AND_RESTORED` while any captured channel is unverified
   (AUD-002/AUD-007).
4. The capability cache is keyed by identity fingerprint + **provider revision + provider artifact
   digest** + registry digest + format; refusal cases are implemented and tested, including
   **quarantine of foreign/mismatched files (bytes preserved) and atomic writes**; a missing required
   identity field disables writes/mutations/restore and forbids validated-profile caching
   (AUD-008/AUD-012).
5. The Vital provider reproduces, through the V3 contracts, the POC's measured invariants:
   ident-addressed parameter writes, document-before-parameters order, one-route-per-state-write,
   per-destination depth conversion with honest `no law` refusals, dependency refusals before writes
   — **and the corrected targeting rule: only the selected block is ever read, written or verified**
   (AUD-001).
6. Universal Core has zero imports of `reapy` and zero Vital identifiers; **inside `plugins/`**, only
   `plugins/host/reaper.py` imports `reapy`; importing the Vital provider in a clean process pulls in
   no `reapy`/`ReaperController`/`_bh_session`/`_b4_lib` and does not change the working directory
   ([12 §2](12-testing-strategy.md), AUD-005/AUD-013).
7. The full offline suite (unit + contract + mock) passes without REAPER installed; live tests are
   separately gated and documented, and no live-write case may claim "restored" without comparing
   every declared captured channel (AUD-015).
8. **No state read, write or verification ever addresses a block other than the one uniquely
   correlated to the validated target**; an ambiguous correlation is a zero-write refusal, and
   untouched blocks remain byte-identical (AUD-001 — P0 gate before any live state mutation).
9. Target identity and mutable state revision are separate mechanics: a stale/mismatched target token
   refuses (`TargetChanged`); an unexpected external state change refuses (`StateConflict`); the
   residual external-race window is documented and owner-accepted (AUD-003/AUD-010, Q-12).

## 7. Approval gates and workflow

```
Phase A  research + planning (docs 00-17)                                 [done]
Phase B  independent audit by GPT-5.6 in Codex                            [done: 3 Critical / 8 High / 4 Medium]
Phase C  resolve findings; update docs/contracts                          [done: doc 18]
Phase D  consolidate the specification; isolate the real decisions        [done: doc 19]
Phase E  owner approves the four decisions; final planning and handoff     [done: docs 19, 20]
Phase F  implementation session: P0-P10, one subsystem at a time          [NOT STARTED]
         (roadmap: [14-implementation-plan.md](14-implementation-plan.md);
          handoff: [20-implementation-handoff.md](20-implementation-handoff.md))
```

Additional rules carried from the brief:

- No implementation before the owner's approval of the four decisions (recorded 2026-10-10 in
  [19 §6](19-owner-decisions.md)); documentation and read-only investigation only in Phases A–E.
- Implementation happens in a **separate session** from the handoff in
  [20](20-implementation-handoff.md); approving the decisions is **not** authorization to start
  coding inside a planning session.
- If implementation reveals a missing contract, the contract change is **documented first**
  (e.g. in [15](15-risks-and-open-questions.md)), not silently coded.
- Each implementation phase ships with its tests and acceptance criteria from [14](14-implementation-plan.md),
  and stops at the ten global gates ([14 §Global phase gates](14-implementation-plan.md#global-phase-gates)).
- GPT-5.6 Codex may independently review significant changes during implementation.

## 8. Ownership

| Role | Responsibility |
|---|---|
| Project owner (human) | Approved the four V1 decisions (2026-10-10), accepts the documented limitations, authorizes live-write testing, gives final sign-off on the package |
| Primary planner (DeepSeek, Phases A–E) | Research, architecture, contracts, plans, evidence index, handoff package |
| Independent auditor (GPT-5.6 Codex, Phase B) | Verifies claims against source; challenges boundaries |
| Implementer (Phase F, separate session) | Builds subsystem-by-subsystem under the approved contracts and the [20](20-implementation-handoff.md) instructions |
