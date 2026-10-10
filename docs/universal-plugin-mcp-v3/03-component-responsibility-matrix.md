# 03 — Component Responsibility Matrix

(rev. 2026-10-10, Phase C remediation → Phase D consolidation → Phase E approvals and handoff — see [18](18-remediation-register.md), [19](19-owner-decisions.md), [20](20-implementation-handoff.md); rows corrected for
AUD-001/AUD-005/AUD-008/AUD-012.)

Purpose: classify every significant component of the POC and the existing MCP, and recommend
reuse / refactor / redesign / drop / investigate for V3. Verdicts follow the project rule: proven
behavior is preserved only after its dependencies and boundaries are examined.

Legend — columns per the project brief. `U` = Universal (plugin-agnostic mechanics), `V` = Vital-specific,
`R` = REAPER-specific, `M` = mixed/intermediate. Transplant verdicts:
**REUSE** (port with edits as proven), **REFACTOR** (extract generic part, leave plugin part),
**REDESIGN** (keep the lesson, new implementation), **DROP** (do not carry), **INVESTIGATE** (needs
Phase-E evidence first).

## 1. Matrix

| Component | Current location | Responsibility | U | V | R | Needs rewrite? | Transplantable? | Evidence / notes |
|---|---|---|---|---|---|---|---|---|
| `mcp/results.py` envelope | `vital/mcp/results.py` | ok/fail/dry envelope, schema v1, sorted JSON | ● | | | No | **REUSE (copy-adapt)** | Envelope is plugin-agnostic; 3 tiny functions [A]. V3 adds `meta.instance`, per-item verification rows |
| `mcp/server.py` registration pattern | `vital/mcp/server.py` | SPEC-driven closure registration + required-arg validation + synthesized docs | ● | | | No (pattern) | **REUSE (pattern)** | `server.tool(name=name)(tool)`; SPEC validation before dispatch; proven with same `mcp[cli]` FastMCP [A] |
| `mcp/tools.py` tool bodies | `vital/mcp/tools.py` | 24 tools, thin; dispatch table | M | ● | | Yes (re-express) | **REFACTOR** | Tool *shapes* (batch-first write, dry-run, per-op read) inform V3; names/specs are Vital's [A] |
| `adapter/core.py` Adapter | `vital/adapter/core.py` | inspect/read/write orchestration, no host knowledge | M | ● | | Partly | **REFACTOR** | The orchestration split (capability vs transaction path) is universal; the module vocabulary is Vital's [A] |
| `adapter/llm.py` typed request layer | `vital/adapter/llm.py` | preflight/submit/plugin_* bulk surface, space-correct verification | ● | ● | | Partly | **REUSE (concepts)** | Contains the flagship lessons: preflight refusals with `host_writes:0`; space-correct compare; batch read-back `read_cost_next:0` [A] |
| `adapter/model.py` (`VitalInstance`) | POC | identity fields + fingerprint | M | ● | ● | Yes | **REFACTOR** | Extract generic `PluginInstanceRef`; keep provider payload (class hex, module string, version) [A] |
| `adapter/model.py` (`Control`, `Module`, `Patch`, `WarpCurve`, `ModulationRoute`) | POC | domain objects | M | ● | | Partly | **REFACTOR** | `Control`/`Module` core fields are generic candidates; route field set is Vital's; `WarpCurve` is Vital's [A] |
| `adapter/controls.py` | POC | control record + conversion funnel | M | ● | | Yes (re-express) | **REFACTOR** | Keep the "one conversion point per control" rule and `ident` binding; laws become provider data [A] |
| `adapter/spaces/laws.py` | POC | conversion laws + stated domains + fit | V(mechanism) | ●(numbers) | | Partly | **REFACTOR** | Law *kinds* + fit priority + `evidence_strength` are generic; constants/ranges are Vital's [A] |
| `adapter/registry.py` | POC | loads 4 generated JSON tables, `suggest()`, wrapper refusal | M | ● | | Partly | **REFACTOR** | Generic idea: provider-owned tables loaded once + name resolution; the data is Vital's, read from a configured data directory (not vendored by default — [19 Decision 2](19-owner-decisions.md)) [A] |
| `adapter/capabilities.py` | POC | groups, `describe_control`, `depth_fit`/`modulation_depth` | M | ● | | Partly | **REFACTOR** | Depth per-destination fit + honest refusals = provider hook; group concept = profile [A] |
| `adapter/capability_cache.py` | POC | cache keying (identity+digest+format), 5 refusal classes, content hash + count cross-check | ● | | | Partly | **REUSE (concepts; 2 behaviours corrected)** | Keying/refusal discipline is the strongest transplant candidate [A]. **Not copied as-is (AUD-008/AUD-012):** `save()` overwrites whatever sits at the path without validating the previous file's identity and writes non-atomically ([02 §7](02-vital-poc-audit.md)); the V3 key must also digest behaviour-affecting **provider** artifacts, not only registry JSON ([09 §3](09-discovery-and-cache.md)) |
| `adapter/dependencies.py` | POC | rule predicates → findings; DISABLE set | M(engine) | ●(rules) | | Partly | **REFACTOR** | Keep finding model + `raise_on` pre-write; rules become provider-declared data/code [A] |
| `adapter/modulations.py` | POC | route select/plan/apply/snapshot/restore, slot identity | M | ● | | Partly | **REFACTOR** | Universal: "route ops through the state codec, never parameters"; Vital: 64 slots/7 fields [A] |
| `adapter/plan.py` | POC | change doc → validated plan; projected-world checks | ● | ● | | Partly | **REUSE (concepts)** | Projection-against-live-state lesson is universal; control naming/roots are provider's [A] |
| `adapter/transaction.py` | POC | snapshot→apply→verify→commit\|rollback; rollback verified for **captured channels only** (document+routes; parameters are a named gap — AUD-002) | ● | ● | ● | Partly | **REUSE (concepts, re-express)** | The single most valuable component. Extract engine; provider declares order/channels/tolerances; V3 closes the parameter gap by complete capture |
| `adapter/executor.py` | POC | op-stream authorise gate + per-op restore, verdict taxonomy | ● | ● | | Partly | **REFACTOR** | Verdict vocabulary (`REFUSED_BEFORE_ANY_WRITE`, `FAILED_AND_(NOT_)RESTORED`) is universal; op vocabulary is Vital's [A] |
| `adapter/wavetables.py` | POC | state-level structural ops on oscillator tables | | ● | | n/a | **DROP from universal** | Keep as provider capability `state.structural_ops`; growth-refusal evidence per plugin [A] |
| `adapter/diagnostics.py`, `core.diagnose` | POC | what to check when nothing works | M | ● | ● | Maybe | **INVESTIGATE** | Useful; depends on bridge internals; not required for V1 |
| `adapter/registry_gen/*` | POC | generators for derived registries | | ● | | n/a | **DROP from core** | Provider tooling, stays where it is [A] |
| `errors.py` | POC | typed error taxonomy | ● | ● | ● | No | **REUSE (adapt)** | Class list maps 1:1 into V3 with provider/host categories added [A] |
| `bridge/bindings.py` | POC | probed call-shape table + timeout/wedge/dialog chokepoint | | | ● | Partly | **REFACTOR** | `call()` shape is universal host technique; probe specifics are Vital/host-version-bound [A] |
| `bridge/connection.py` | POC | lazy connect, track accessor, reconnect | | | ● | Partly | **REUSE (pattern)** | Lazy connect + typed failures; V3 host bridge re-implements against the repo's reapy usage [A] |
| `bridge/locate.py` | POC | enumerate/resolve/fingerprint/version gate | | ● | ● | Yes | **REDESIGN as provider+hoster split** | Generic: fingerprint compare-on-use, never-GUID, ambiguity raises. Vital: name/class hex/module strings [A] |
| `bridge/parameters.py` | POC | inside-REAPER batch IO, ident-addressed, read-back incl. ident | | | ● | Partly | **REFACTOR** | Batch shape + ident-inside-hop + no index cache are generic techniques; V3 re-implements on `reascript_api` [A] |
| `bridge/state.py` | POC | track-chunk transport **+ Vital document codec** (`_b4_lib`) | M | ● | ● | **Yes** | **SPLIT (REDESIGN, not plain refactor)** | Transport (targeted slice extract/splice + settle/retry + whole-chunk write) → Host Bridge; document codec (base64 JSON, framing) → Vital state codec [A]. **Targeting defect (AUD-001):** `select_block`'s result is reporting-only; `parse_framing`/`rebuild_chunk` operate on the first `<VST>` block of the full chunk ([02 §10](02-vital-poc-audit.md)). V3 requires a `TargetedStateSlice` contract — the bridge must extract/reinsert exactly the selected block span and the codec must operate on that slice ([10 §1](10-state-modulation-dependencies.md)) |
| Legacy `_b4_lib` / `_bh_session` / `_b3_lib` | `C:\REAPER\prompts\vital_sound_design\scripts\` | byte framing + legacy host session | M | ● | ● | **Depends on isolation decision** | **DO NOT IMPORT into Core or the provider (AUD-005)** | module-level `import _bh_session` → module-level `import reapy`, `os.chdir(...\src)`, `from reaper_controller import ReaperController`, `vital_*` imports ([02 §15](02-vital-poc-audit.md)). Isolation approach: port the verified host-free function set ([17 AV-13](17-planning-evidence-index.md)) into the provider codec; fallback is a clean re-implementation against the same fixtures ([08 §6](08-provider-and-host-interfaces.md), [19 Decision 2](19-owner-decisions.md)) |
| `bridge/dialogs.py` | POC | Win32 dialog watchdog | | | ● | Partly | **REUSE (concepts; opt-in)** | Blocks-all-calls fact is universal for reapy; V3: optional, config-gated, never auto-clicking questions [A] |
| `bridge/fxchain.py` | POC | FX load/remove/repair, VST2/VST3 discriminator | | ● | ● | Partly | **INVESTIGATE** | Repair is operationally useful but outside V1 scope; VST2/VST3 discrimination lesson is real [A] |
| `bridge/README.md` contract | POC | "bridge knows where/how, never what" | ● | | ● | No | **REUSE (as V3 host-bridge contract text)** | Five capabilities + forbidden list [A] |
| `cli/main.py` | POC | second consumer of tool functions | ● | ● | | n/a | **DROP for V3 V1** | V3 is MCP-first; a CLI could return later as a test harness (brief §17) |
| `_env.py` legacy import shim | POC | `REAPY_MCP_SRC`, cwd-preserving legacy import | | ● | ● | n/a | **INVESTIGATE (dependency risk)** | Load-bearing for POC state codec; V3 must not depend on `os.chdir`-ing legacy modules [A] |
| POC test scripts (32) + probes (~65) | POC | layered tests + statistical experiments | M | ● | ● | No | **REUSE (as regression oracle, not as suite)** | V3 re-expresses the *lessons* in pytest with mocks; POC suite stays the Vital regression oracle [A] |
| `probe_common.py` two-writer rule | POC | `write_json` (stable) vs `write_run_json` (per-run) | ● | | | No | **REUSE (concept)** | Same evidence-identity distinction as `sound_design_kb/evidence_identity.py` [A] |
| `sound_design_kb/` | C:\REAPER | plugin-agnostic sound-design knowledge | ● (but not Core's job) | | | n/a | **OUT OF SCOPE** | Explicitly never loaded at runtime; belongs to the LLM knowledge tier, not the plugin layer [A] |
| REAPER MCP `mcp_tools.py` | target repo | 57 host-level tools | | | ● | No | **REUSE unchanged** | V3 adds tools beside them; no changes to existing 57 [A] |
| REAPER MCP controllers | target repo | host operations, swallow-exception semantics | | | ● | Out of scope | **DO NOT REUSE in plugin path** | Plugin tools must not inherit `{"status":"error"}` string semantics or bool sentinels [A] |
| `server.create_server()` | target repo | the integration point | | | ● | Small addition | **EXTEND** | Add `setup_plugin_tools(mcp, ...)` call [A] |
| `reaper_side_enable_server.py` | target repo | enables reapy dist API inside REAPER | | | ● | No | **REUSE unchanged** | Prerequisite for any live host access [A] |

## 2. Verdict summary

**Transplant near-verbatim (adapt names, keep logic):** `results.py` envelope, `capability_cache`
keying/refusal discipline, error taxonomy shape, registration/validation pattern, host-call
chokepoint concept, two-writer evidence rule.

**Extract-and-generalize (the real engineering work):** transaction engine, plan builder,
dependency-finding model, route/state-codec operation model, instance identity (host + provider
payload), value-space authority declaration, host bridge transport split from the Vital codec.

**Leave behind (Vital-specific; lives in the provider package or the POC data tree, never in Core):** locator strings and measured versions,
base64-JSON document codec internals, laws/constants/corpus ranges, vocabularies, DISABLE-set rule
content, wavetable mechanics, registry generators, CLI.

**Do not carry (dropped deliberately):** controller-based error semantics; per-control host reads on
the LLM path; index-addressed parameter writes; any dependency on `os.chdir`-ing legacy imports;
reliance on `SetTrackStateChunk`'s return value.

## 3. Explicit non-goals recorded here

- No component is copied into the repository during planning. Porting decisions are executed only
  in the implementation session (P0–P10) under [14](14-implementation-plan.md), and in V1 the Vital
  provider is an **in-repo,
  dependency-isolated provider package** ([08 §6](08-provider-and-host-interfaces.md),
  [19 Decision 2](19-owner-decisions.md)) — **without importing the legacy `_b4_lib` module
  chain** ([02 §15](02-vital-poc-audit.md), AUD-005).
- The POC's measured constants are **evidence**, not constants to hard-code into Core
  (e.g. 0.4 s settle, ~2.3 s/route, 680× batch ratio) — they parameterize provider profiles and
  test budgets only.
- **The POC's target-state behavior was measured on single-Vital-block tracks.** Its state
  read/write path is **not** evidence that multi-VST targeting is safe ([02 §10](02-vital-poc-audit.md),
  AUD-001); the V3 `TargetedStateSlice` mechanism is new work with its own P0 tests.
