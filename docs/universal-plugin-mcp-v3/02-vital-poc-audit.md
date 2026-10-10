# 02 — Vital POC Audit (`C:\REAPER`)

Status: **[A]-based audit, rev. 2026-10-10 (Phase C remediation → Phase D consolidation → Phase E approvals and handoff — see [18](18-remediation-register.md), [19](19-owner-decisions.md), [20](20-implementation-handoff.md)).**
Sections corrected in Phase C: §5 (identity machinery absence), §7 (cache save behaviour),
§10 (state targeting defect), §12 (rollback scope), §15 (legacy import chain), §16 (L4 restore
scope), §19 (open questions).

Scope: the working POC that proves the mechanics V3 generalizes. Code:
`C:\REAPER\Scripts\sound_design\vital\` (`vital` package). Data & documents:
`C:\REAPER\prompts\vital_sound_design\` (registry, logs, architecture docs). Knowledge base:
`C:\REAPER\sound_design_kb\`. All claims **[A] verified in source** unless tagged otherwise;
**[C]** = recorded run/log evidence; **[D]** = inference/unresolved.

## 1. Package map

```
vital/
├── __init__.py, _env.py            paths + legacy-import bridge (cwd-safe)
├── errors.py                       typed error taxonomy (20 classes)
├── adapter/                        THE ONLY Vital-aware layer (may not import reapy, test-enforced)
│   ├── __init__.py                 exports Adapter, Control, Patch, Transaction, Executor, ...
│   ├── core.py                     class Adapter (inspect_*, control_value, resolve, diagnose, ...)
│   ├── model.py                    VitalInstance, Control(+row), Module, WarpCurve, ModulationRoute, Patch
│   ├── controls.py                 Control.from_row; conversion funnels through spaces/laws
│   ├── registry.py                 loaded JSON tables: correspondence, vocabulary, controls, scales
│   ├── capabilities.py             LLM ask-surface: groups, describe_control, depth_fit/modulation_depth
│   ├── capability_cache.py         measured capability map cached per identity+registry digest
│   ├── spaces/laws.py              conversion laws (affine/enum_index/exponential/identity/none)
│   ├── dependencies.py             measured rule predicates → findings (hard/soft/incompatible)
│   ├── modulations.py              routes: select/plan_create/update/remove, snapshot/restore
│   ├── plan.py                     change document → validated plan (+ DISABLE set)
│   ├── transaction.py              snapshot → apply → verify → commit | rollback
│   ├── executor.py                 op-stream executor (authorise gate, per-op restore)
│   ├── llm.py                      typed request surface: preflight, submit, plugin_* bulk tools
│   ├── wavetables.py               state-level structural ops on oscillator tables
│   ├── diagnostics.py              what to look at when nothing works
│   └── registry_gen/               generators for the derived registries
├── bridge/                         THE ONLY REAPER-aware layer
│   ├── connection.py               single lazy reapy link; track(track_index); reconnect()
│   ├── bindings.py                 probed call-shape table + timeout/wedge/dialog chokepoint call()
│   ├── locate.py                   enumerate / resolve / fingerprint / version gate
│   ├── parameters.py               batched param IO executed INSIDE REAPER via @reapy.inside_reaper
│   ├── state.py                    track-chunk document read/write/verify (Vital codec lives here)
│   ├── dialogs.py                  Win32 dialog watchdog (modal boxes block all reapy calls)
│   ├── fxchain.py                  FX load/remove/repair (VST2 vs VST3 discrimination)
│   └── README.md                   the bridge contract ("the dumbest component")
├── mcp/
│   ├── server.py                   FastMCP("vital") stdio; explicit SPEC-driven registration
│   ├── tools.py                    24 tool functions + SPEC + DISPATCH + TOOL_NAMES
│   └── results.py                  ok()/fail()/dry() envelope, schema version 1
├── cli/main.py                     `vit` — a SECOND consumer of the same tool functions
└── tests/                          32 test scripts + ~65 probes/dry-runs (see §13)
```

## 2. Runtime chain (verified)

```
LLM / agent
  → Vital MCP server  (mcp/server.py, FastMCP("vital"), stdio at run())
     or `vit` CLI     (cli/main.py — same tool functions via T.DISPATCH)
  → tools (mcp/tools.py)        24 tools; envelope from mcp/results.py
  → Adapter (adapter/core.py + llm.py + plan/transaction/executor)   Vital-aware, REAPER-free
  → bridge (locate/parameters/state/bindings/connection)             REAPER-aware, Vital-free per chart
  → REAPER (reapy, in-REAPER batch execution) + Vital VST3
```

The earlier forensic claim that "the LLM used the CLI, not a running MCP server, and the CLI calls
the same tool functions" is **confirmed** [A]: `cli/main.py` imports `from vital.mcp import tools as T`
and dispatches to `T.*` inside every `cmd_*`, printing `results.dumps/summarize` output — so `--json`
is the tool envelope verbatim. CLI-only escape paths that bypass tool functions exist
(`cmd_state`, `cmd_validate`, `cmd_fx`, `cmd_dialog`, `cmd_presets`) and are documented as CLI-only [A].

**Lazy connect [A]:** the MCP server constructs the Adapter but connects to REAPER only on the first
host-needing call (`server.py` docstring: an `inspect_vital_instance` answering "not found" is a
useful answer). Both entrypoints start the dialog watcher first (`dialogs.start(autodismiss=True)`)
before any host call [A: server.py:108; cli/main.py:~903].

**Why separate from the existing REAPER MCP [A]:** `vital/mcp/server.py` docstring records the two
measured reasons: the existing MCP's controller methods swallow exceptions
(`except Exception: return []`) so "silence is not proof of success"; and it has no batch parameter
primitive and no preset/modulation surface, so Vital tools would inherit a contradictory failure
semantics. Recorded as DEC-01 in `prompts/vital_sound_design/architecture/DECISIONS.md` [A].
This is the decision V3 revisits — conditionally — in [13](13-existing-mcp-integration.md).

## 3. MCP surface (24 tools)

Registration is **explicit, not decorator-per-function** [A: mcp/server.py:57-95]: `build_server()`
loops `T.TOOL_NAMES`, wraps `T.DISPATCH[name]` in a generic `def tool(**kwargs)` closure that
(a) validates required args from `T.SPEC[name]` before dispatch, (b) converts `TypeError` into a
typed `InvalidValue` envelope, (c) synthesizes `__doc__` from `SPEC` (purpose/args/destructive), and
registers with `server.tool(name=name)(tool)`.

Tool names (verbatim from `TOOL_NAMES`, tools.py:45-61) [A]:
`inspect_vital_instance`, `inspect_patch`, `inspect_section`, `inspect_modulation`,
`apply_patch_changes`, `apply_parameter_changes`, `apply_state_changes`, `create_modulation`,
`update_modulation`, `remove_modulation`, `snapshot_state`, `restore_state`,
`plugin_describe`, `plugin_module`, `plugin_find`, `plugin_read`, `plugin_write_batch`,
`plugin_wavetables`, `capability_groups`, `capability_group`, `control_capability`,
`modulation_depth`, `request_preflight`, `apply_request`.

> **Discrepancy recorded:** `vital/README.md` says "mcp/tools.py — the 12 tools". The actual registry
> has **24** names; the first 12 are the Phase-3 surface, and the `plugin_*` / `capability_*` /
> `request_*` names were added later. The README is stale on this point; the code is authoritative [A].

Envelope [A: mcp/results.py]: plain dicts, `SCHEMA_VERSION = 1`:
- `ok(tool, data, warnings=None, meta=None)` → `{"tool", "schema": 1, "ok": true, "data", "warnings": [], "meta": {}}`
- `fail(tool, error, detail=None)` → `{"tool", "schema": 1, "ok": false, "error": {"code", "message", "detail"}, "warnings", "meta"}`; a `VitalError` becomes `error.to_dict()`, anything else `code="unexpected_error"`.
- `dry(tool, plan, ...)` → same + `"dry_run": true`, `data = plan`.
- `dumps()` = sorted-key JSON (byte-comparable output shared by CLI/MCP).

Deliberate asymmetry [A]: `plugin_write_batch` defaults `dry_run=False` (batch-first, internal
preflight); `submit`/`apply_request` default `dry_run=True`. Documented in `llm.py` as intentional.

## 4. Adapter API (adapter/core.py + llm.py)

`class Adapter` [A: core.py:52]: `resolve`, `addressing`, `inspect_instance`, `sweep_controls`,
`read_controls`, `control_value`, `inspect_section`, `inspect_patch`, `inspect_modulation`,
`list_presets`, `diagnose`. The LLM-facing typed-request layer lives in `adapter/llm.py`:
`request_preflight` (:479), `submit` (:769), `plugin_describe` (:927), `plugin_module` (:962),
`plugin_find` (:977), `plugin_read` (:1007), `plugin_write_batch` (:1106), `_verify_batch` (:1182).

**`submit()` flow [A: llm.py:769-905]** (verified by direct read):
1. `request_preflight(request)` — pure; failures return `refused_before_write` with
   `host_reads: 0, host_writes: 0`. An actionable refusal, never a silent no-op.
2. Build the change document (label/structural/controls + routes_*).
3. Wavetable specs are resolved **against the live document** (`wavetables.change`); failure →
   `stage: "wavetable_resolve"` refusal, nothing written.
4. `resolve_routes_against(adapter, request)` reads the **live** route table and passes it into
   `plan.build(document, current_routes=...)` — the docstring records that an omitted argument made
   a create see 64 free slots / an update address slot 1 of a zero-length list (a recorded defect).
5. `Transaction(adapter).begin(...)` → `plan_document(...)`, `plan_parameters(...)`.
6. If `dry_run`: `tx.dry_run()` preview (`would_write`, per-item problems; `raise_on` NOT called so
   hard findings are reported rather than raised).
7. Else `tx.apply()` → verdict; output carries `ok/committed/stage/route_plan/plan_summary/verdict`
   and, on refusal/rollback, the explicit note that "the instance is reported unchanged".

**`plugin_write_batch()` [A: llm.py:1106-1158]**: internal `request_preflight` → `submit(dry_run=False)`
→ `_verify_batch(...)` (one batched parameter read + one document read restricted to the touched
route slots) → response contains `verification.controls` / `verification.routes` / `rejected` and
`read_cost_next: {"needed": 0, ...}` ("this response already contains the read-back of everything it
changed"). `_verify_batch`'s docstring records a fixed defect class: the first version compared a
read-back normalized value against the requested **display** value and reported three false
mismatches on a landed batch; the fix was comparing "in the space that was written" by re-resolving
through the same `transaction._resolve_value` the write used — "the fix is the SPACE, not the
tolerance" [A].

Capability tools read the cached capability map, never the host [A: llm.py:935-994 calls
`capability_cache.identity_of/key_for/module_names/module_map/find`].

## 5. Instance identity & resolution

Identity components [A: bridge/locate.py; adapter/model.py:88 `VitalInstance`]:
`track_index`, `fx_index`, `fx_name`, `vst_class` (chunk class hex), `module` (`vital.vst3` vs
`vital.dll`), `param_count`, `synth_version`, plus a recorded-but-unused `fx_guid_pointer`.

- Resolution enumerates every track × FX; a candidate is any FX whose name **contains** "Vital"
  (substring), then class id from the chunk `<VST …>` header + module name match identifies the
  measured build [A: locate.py:166-215].
- Zero candidates → `InstanceNotFound` (with a special message for the VST2 build); more than one →
  `AmbiguousInstance` unless a selector is given (`track_index|fx_index|track_name|name`) — the bridge
  never picks among candidates [A].
- **GUID is read but explicitly never used as identity** (raw process pointer) [A: locate.py:36-40, 203-207].
- A fingerprint (`n_params`, `fx_name`, `vst_class`) is re-checked at use; a mismatch re-resolves
  once, then raises `StaleInstance` [A: locate.py:396+].
- Version gate: `MEASURED_VERSIONS = ("1.5.5",)`; a version not in the measured set produces
  `VersionMismatch` where `assert_version` is called; `resolve_instance(validate_version=True)`
  records a `version_warning` [A].
- Parameter addressing is **VST3 ident-always** (`"104:48629"` shape) resolved *inside each hop*
  (`TrackFX_GetParamFromIdent`); there is deliberately **no client-side index cache** [A:
  parameters.py:60, 96-160, 275].
- **No write-target token and no state-revision machinery exists anywhere in the POC** [A, from
  absence: no token/revision/digest-of-live-state concept appears in `locate.py`, `state.py` or
  `transaction.py`]. The POC's protection against a replaced instance is only the *fingerprint
  compare-on-use* (re-resolve once → `StaleInstance`). Whether that can distinguish an
  identical-build replacement in the same slot is **unresolved** [D]; the POC's live measurements
  never exercised that case. V3 therefore specifies target-token and state-revision concepts as
  **new contracts** ([05 §5a](05-universal-core-contract.md), [09 §2](09-discovery-and-cache.md));
  it does not inherit them from the POC.

## 6. Value spaces, control records, and laws

The central rule [A: README.md §Value spaces; model/docstrings]:
**a control value is read only from the parameter array; the document is a snapshot** (the plugin
does not re-serialise on a parameter change). `tests/test_l1_pure.py` asserts the code paths honour it.

- `controls.Control` fields [A: controls.py:46]: `name, index, module, state_key, kind, nmin, nmax,
  n_states, discrete, duplicate_name, evidence_strength, evidence_source, value, ident`.
- Built from registry rows (`Control.from_row`), including the **`ident`** (VST3 parameter ident) and
  numeric `index`; `kind ∈ {affine, enum_index, exponential, identity, none, unmeasured}`.
  `none`/`unmeasured` are first-class "do not convert" answers [A: spaces/laws.py].
- Conversion is funnelled: `Control.native_of(norm)` / `norm_of(native)` → `laws.to_native/to_normalized`;
  domain ranges come from `STATED_RANGES` (modulation_amount −1..1, power −10..10, log2 rate/period)
  or measured display endpoints, with fit priority enum → stated → display endpoints → corpus range
  → identity, and `evidence_strength` recorded [A: spaces/laws.py].
- Display strings are derived **offline** from `vital_scales_full.json` scales, not from
  `TrackFX_GetFormattedParamValue` (≈9-10× host cost) [A: registry.py docstring; parameters.py].
- **No alias table** exists in `controls.py` [D from absence]; name resolution has a `suggest()`
  (difflib + token + module-prefix boost) and `plan.resolve_control_name` accepts registry name OR
  state key with pretty-case fallback, refusing with suggestions otherwise [A].

## 7. Capability map & cache

`adapter/capabilities.py` owns the derivation (groups, per-control description, depth fitting);
`adapter/capability_cache.py` caches its output [A].

- **Rationale (recorded)** [A: capability_cache.py docstring]: a forensic accounting of a real
  run (2026-09-28) counted 192 Adapter calls, 102 capability calls, 98 of them about ONE control;
  ~110 calls were answerable from static data already held. "The operator was not doing synthesis;
  it was paying a per-control tax."
- **Key** [A]: identity fields `(fx_name, vst_class, synth_version, param_count, track_index,
  fx_index)` with **`vst_class` + `param_count` required**; PLUS a **digest of the four registry
  files** every number derives from; PLUS `CACHE_FORMAT_KEY = "capability-map-v1"` / `CACHE_SCHEMA = 1`.
  "That is the difference between a cache and a lie" — a regenerated registry must not serve
  yesterday's ranges.
- **Five refusals** [A: docstring + `build_or_load` + `save`]: (1) no usable identity → build in
  memory, no cache; (2) no file → build+write; (3) unreadable/unparsable → build, write at the same
  path, say why; (4) a payload **with a different key** found at the expected path **cannot arise
  through the normal flow** (the filename is derived from the key hash) — it arises only if a
  foreign file was placed there externally; (5) content-hash / registry-digest / format mismatch →
  REFUSE the file, rebuild, report.
- **Correction of a Phase-A overclaim (audit AUD-012) [A]:** the Phase A wording *"the old file is
  left byte-identical and the new map is written at its own name"* is true only as a consequence of
  the filename carrying the key — **it is not enforced by `save()`**. `save()` (`capability_cache.py:408-417`)
  reads whatever exists at the path only *for reporting* (`replaced: previous is not None`) and then
  **overwrites it with a direct `open(path, "w")`** — there is no check that the previous file is
  the same key, no quarantine, and **no temp-file + atomic rename**. In the refusal path
  (`build_or_load` case 5) the rebuild concludes with a `save()` at the same path, so a file that
  just failed validation **is overwritten**. Foreign-payload preservation and atomic writes are
  therefore **new V3 requirements** ([09 §4-§5](09-discovery-and-cache.md)), not inherited behavior.
- A truncated file fails **two** checks (content hash + recomputed `n_controls` cross-check) — "an
  empty or partial read must not be able to pass as a complete one" [A].
- Storage: `prompts/vital_sound_design/logs/capability_cache/` — "no knowledge: a derived table that
  can be deleted at any moment and will be rebuilt" [A]. `_MAP_CACHE`/`_DIGEST_CACHE` memoise per
  process [A].
- Consumer surface: `module_names`, `module_map`, `find` (search) served from the cache [A: llm.py:935-1006].

## 8. Modules and grouping

`capabilities.CAPABILITY_GROUPS` presents 7 groups to the LLM [A]: `oscillator` (Osc 1-3 + Sample),
`filter` (Filter 1/2 + Filter FX), `envelope` (6), `lfo` (8 + 4 Random), `modulation` (64 slots),
`effect` (Chorus, Compressor, Delay, Distortion, EQ, Flanger, Phaser, Reverb, Macro), `global`;
plus `PATCH_CONTROLS` and `SHORT_FORM_MODULES`. Grouping is derived from the registry `module`
field, never index adjacency [A: model.py `MODULE_PREFIXES`/`classify_module` docstring].
> Docstring in `capabilities.py` says "twelve capability groups" while the literal shows 7 [D — count
> discrepancy recorded, harmless but noted].

## 9. Modulation

[A: adapter/modulations.py, model.py:349, capabilities.py depth functions]

- `ModulationRoute(slot, source, destination, amount, bipolar, stereo, power, bypass, line_mapping,
  dependencies)`; **slot (1..64) is the only identity** — no id field; operations are list-oriented
  with deterministic first-match-in-slot-order and explicit `remaining` reporting [A].
- All **seven route fields are written through the document channel**
  (`settings.modulations[i].*`, `settings.modulation_{n}_{field}`); a parameter write alongside a
  routing write does not take [A: modulations.py `ROUTE_FIELDS`, `apply_changes_to_document`].
- Route writes: `plan_create/plan_update/plan_remove` with sequential simulation so a second create
  sees the first one's slot taken; `snapshot_routes`/`restore_routes` support rollback and clear
  un-snapshotted slots [A].
- **Semantic depth → native amount is provider math, per destination** [A: capabilities.py]:
  `depth_fit(destination)` least-squares-fits the destination's measured display states, yielding
  `native_per_unit = k`; `modulation_depth(destination, depth)` → `amount = depth / k`. Refused where
  the display is not affine in normalized (worst residual > ½ display quantum) — 246/316 destinations
  pass; refusals include all 8 LFO frequency destinations, `Volume`, `Reverb Decay Time`,
  `Osc 1 Unison Detune` [C in-code measurement].
- Vocabulary gate: 31 sources / 316 destinations with a source-family precedent rule
  (no LFO → `modulation_N_amount`), `force`-overridable [A: registry `vocab().validate`].
- `LINE_MAPPING_WRITE_SUPPORTED = False`: a route carrying a curve does not land; a curve added to a
  landed route is silently stripped by the write channel on 1.5.5 — the POC refuses requested curves
  and warns that an edit drops them [A: README.md §Two measured constraints; DEC-25].

## 10. State/document channel and the codec location

- `bridge/state.py` [A]: reads the track chunk (`GetTrackStateChunk` via `bindings.chunk`), locates
  the Vital `<VST>` block by class hex + ordinal among Vital FX on the track (`select_block`), and
  decodes a **base64-wrapped JSON preset document**; byte framing/rebuild is delegated to a legacy
  Phase-2 module (`_b4_lib`) imported via `_env.import_legacy` (cwd-preserving) [A].
  `write_document(addressing, doc, *, label, settle_s=0.4, retry_s=3.0, verify=None, expect=None,
  ...)` → `{ok, applied, attempts, verified, mismatched, elapsed_s}`; **`SetTrackStateChunk`'s return
  value is recorded but never trusted**; verification is by decode-and-compare [A].
- **Targeting limitation — confirmed defect, audit AUD-001 [A].** The block identified by
  `select_block` is used **only for reporting** (`block_header`, `block_class_hex`,
  `block_is_first_vst` in `read_document`'s return). The byte path ignores it:
  `read_document` passes the **full track chunk** to `b4.parse_framing(chunk)` (its comment says
  "rebuild a single-block chunk view", which the code does not do), and `_b4_lib.find_vst_block`
  (`_b4_lib.py:108-111`) selects the **first** `<VST ` block in the chunk
  (`next(k for k, l in enumerate(lines) if l.startswith("<VST "))`). `rebuild_chunk` splices around
  that first block's `head_lines`/`tail_lines`, and `_set_chunk` writes the whole chunk back.
  Consequences on a track with more than one VST block: a read/decode/verify addresses the **first**
  VST block, not the selected instance; if the first block belongs to another plugin the read
  typically fails at JSON decode, but **if the first block is a different Vital instance, reads and
  writes silently target the wrong instance**. The POC's live-verified state behavior was measured
  in environments where the Vital block was the relevant/first VST block; multi-VST targeting is
  **unproven and, as implemented, unsafe**. V3 defines the `TargetedStateSlice` contract as a P0
  requirement ([10 §1](10-state-modulation-dependencies.md), [11 P0](14-implementation-plan.md)).
- `read_document` returns `{doc, framing, chunk, block_header, block_class_hex, json_offset,
  blob_bytes, chunk_chars, settings_count, top_keys}`; `read_settings` returns the
  `doc["settings"]` dict (781 keys when last measured [C]).
- This module **is a mixed component**: it sits in the bridge (correctly the only place with track
  chunks) but understands the Vital document format. V3's response: keep the *transport* generic and
  move the *document semantics* behind a provider State Codec ([10](10-state-modulation-dependencies.md)).
- Wavetable selection is a **state-level op**: no wavetable parameter exists among 775 measured
  controls; `wavetables.resolve/change/set_table` produce document paths written through the same
  Transaction; growth (adding frames) is refused by default on measured evidence (a growing payload
  did not land and once aborted REAPER) [A + C in-code].

## 11. Dependencies

[A: adapter/dependencies.py]
- Mechanism: **code predicates** in `check_plan(plan, flags, *, structural, requested, routes)`
  returning finding dicts with `kind ∈ {hard, warning, info}` (+ `informational` flags); one data
  table `DISABLE_CANDIDATES` supplies OFF values for the structural DISABLE set.
- Representative rules: FM/RM distortion needs a modulator osc; DIRECT OUT makes planned
  filter/FX changes inaudible (hard, informational); a route into an OFF filter is silent;
  a single-frame wavetable makes Wave Frame inert; percentage-unit traps (`Filter 1 Mix = 1.0`
  means 1 %); "a structural change is a TRANSITION, not an assignment" (needs the DISABLE set).
- Surfacing: `raise_on(findings)` raises `DependencyViolation` / `IncompatibleSettings` for
  non-informational hard findings; called by `Transaction.apply` **before any write**; soft findings
  ride in output as data [A].

## 12. Plan and Transaction (the safety core)

**`plan.build(document, *, current_routes=None)`** [A: plan.py:318] → `{label, structural, controls,
state, routes_create/update/remove, disable_set, warnings, findings, n_controls}`; "Nothing is
written." Validations: control/state resolution (unknown → `UnsupportedControl` with suggestions);
document path rooting at a real top key (`DOCUMENT_TOP_KEYS`) else `PlanError`; dependency findings
computed against the **projected** world (planned values overlaid on a live settings snapshot — the
snapshot's stale flags would otherwise falsely refuse a correct patch) [A].

**`Transaction`** [A: transaction.py; quotes below are from the module]:

```
begin → snapshot → resolve/validate → DOCUMENT write → PARAMETER batch → verify → commit
                                            │                │            │
                                            └────────── any failure → rollback (verified)
```
1. **Order is enforced in code, not configurable** — a `stage_parameters_first` probe path exists
   solely so a test proves the reversed order is refused (`WriteOrderViolation`, transaction.py:607-622)
   [A]. Reason (measured, PHASE 2A): a document write whose content differs from the stored document
   **resets every ordinary parameter** to the document's snapshot values (filter cutoff 0.20→0.54688);
   the document is authoritative and must land first [A][C].
2. **Snapshot** [A:180-192]: `deepcopy(document)` + `framing` + `snapshot_routes(...)` + the planned
   parameter **indices**. Memory only — no file I/O in the module [A].
3. **Document write** [A:358]: ≤1 route slot → one `bridge_state.write_document(...)` with a verify
   predicate; ≥2 slots → **one write per route slot**, each re-reading the live document first
   (measured: ≥2 routes in one state write land nothing; sequential single writes land 8/8 at
   ~2.3 s per route) [A][C].
4. **Parameter batch** [A]: idents resolved (`bridge_params.idents_of`) then a single
   `bridge_params.write(addr, [(ident, normalized)])` executed inside REAPER [A].
5. **Verify** [A:449]: document re-read + `dig(after_doc, path)` per change; floats `abs(got-want) >
   1e-5`; non-floats exact; parameters `abs(got-want) > 1e-3`; optional caller predicate. Returns
   `{ok, problems, channels_checked, why: "success is a verified read-back, never an API return value"}`.
6. **Rollback** [A:503]: restores snapshot document + routes via `write_document(...)` with a
   **verification predicate that re-decodes and compares the route snapshot for equality**; an
   unverified restore raises `RollbackFailed` — "the caller must not be able to mistake this state
   for safe". **Parameters are NOT restored**: the result reports `parameters_restored: False` and a
   `parameter_gap` explanation (DEC-19: "a named gap is a feature"); `_snapshot_summary` reports
   `exhaustive: False` and `not_captured: ["wavetable selection", "loaded sample file",
   "parameter values (document only)"]` [A].
   **Scope of the claim (audit AUD-002) [A]:** this is a verified *document-and-routes* rollback,
   not a full session rollback. Because restoring the document writes a differing document, the same
   measured mechanism that makes the forward order document-first (a document write resets ordinary
   parameters to the document's snapshot values) also applies on the way back: after a rollback,
   live parameter values equal the **restored document's** values, which need not equal their
   pre-transaction **live** values (the plugin does not re-serialise on parameter change, so live
   and document values can differ before the transaction even starts). The POC itself does not claim
   otherwise — but any V3 text claiming "verified rollback" without qualifying the captured
   channels would be false; V3 closes this gap by contract ([11 §2.3](11-planning-transactions-and-verification.md)).
7. **Dry-run** [A:128]: read-side stages only; reports `document_changes`, `parameter_changes`,
   `dependency_findings`, `write_order: ["document","parameters","verify"]`, `n_slots_in_use`,
   `would_write` [A].

**`Executor`** [A: executor.py:664] — a later (Phase 4F/4H) op-stream layer over the same
Transaction: per-operation snapshot(both channels) → write → read-back through the same reader →
compare → commit or restore-and-verify; a pre-write `authorise` gate refuses
(`NO_IDENTITY`, `NOT_EXECUTION_READY`, `NO_OPERATION`, `NO_VALUE`); a refused stream returns
`REFUSED_BEFORE_ANY_WRITE` with `n_writes == 0`; verdicts include `FAILED_AND_RESTORED` vs
`FAILED_AND_NOT_RESTORED` (unverified restore outranks everything) [A]. This layer demonstrates the
"nothing is written when the gate refuses" property that V3 keeps as a core invariant.

## 13. Bridge layer highlights (transport evidence)

- **Single chokepoint [A]:** every host call goes through `bindings.call(fn, *args, timeout=20.0,
  label=...)` — daemon-thread execution, wedge detection (`Wedged`), socket-loss classification
  (`NotConnected`), and dialog-text capture attached to failures.
- **Batch IO inside REAPER [A]:** `parameters._BatchFns` decorated `@reapy.inside_reaper()`;
  `write` resolves ident→index **inside the same hop** (`TrackFX_GetParamFromIdent` →
  `SetParamNormalized`), reads back value **and ident** (`ident_ok`), repairs envelope pins only on
  failure+request. Measured: in-process batch 0.14 ms vs 94.6 ms over the socket (~680×) [C].
- **Post-write settling [A]:** state writes decode-verify, settle 0.4 s, retry at 3.0 s (bridge
  default) / 2.0 s (call from transaction); a wedge becomes `WriteNotApplied(class:"wedged")`.
- **Dialog watchdog [A]:** Win32 (`#32770` class) polling thread; conservative rules when idle,
  aggressive while a host call is in flight; `suppress_reapy_dialogs()` turns off reapy's own
  traceback dialog inside REAPER; capture-before-dismiss text is attached to errors.
  Rationale: a modal dialog blocks **every** reapy call [A: bridge/README.md].
- **FX chain repair [A]:** `fxchain` can load the measured build and remove the wrong one; VST2 vs
  VST3 Vital share class id, so `module` string (`vital.vst3` vs `vital.dll`) is the discriminator —
  this is *why* the module exists.

## 14. Errors

`errors.py` defines `VitalError(code, message, detail, to_dict())` plus 20 classes [A]:
transport (`NotConnected`, `Wedged`, `Unreadable`, `BindingUnavailable`), instance
(`InstanceNotFound`, `AmbiguousInstance`, `WrongPlugin`, `StaleInstance`, `VersionMismatch`),
values (`UnsupportedControl`, `InvalidValue`, `NoConversion`), routes (`InvalidRoute`,
`InvalidSourceFamily`), dependencies (`DependencyViolation`, `IncompatibleSettings`), write outcomes
(`WriteNotApplied`, `StateMismatch`, `ReadbackMismatch`), process (`WriteOrderViolation`,
`RollbackFailed`, `RestoreIncomplete`). `WriteNotApplied` is documented as "thrown on the
**read-back**, never on the return value of the write" [A].

## 15. Registries, laws and generated data

[A: adapter/registry.py, spaces/laws.py, _env.py]
- `_env.REGISTRY = C:\REAPER\prompts\vital_sound_design\registry`; files:
  `vital_correspondence.json` (778 rows; per-control law/native ranges/`fit`/`ident`),
  `vital_controls.json` (778 live-read controls snapshot), `vital_scales_full.json`
  (`scales[name].states = [[norm, display], ...]`), `modulation_vocabulary.json` (sources,
  destinations, pairs, families, rules; generated from a 2226-preset corpus walk),
  `wavetable_init.json` (content-digest self-checked on load) [A].
- Regenerated by `registry_gen/` (correspondence, vocabulary, law_fit*); missing files raise
  `FileNotFoundError` pointing at the generator [A]. **No runtime version/staleness check exists for
  the four main registries** [A from absence] — the only self-check is wavetable_init's digest.
- `_env.REAPY_MCP_SRC = r"C:\repos\reaper-reapy-mcp\src"` [A]: inserted on `sys.path` so *legacy
  Phase-2 scripts* (which `os.chdir` into it at import) can import the existing repo's
  `reaper_controller`; `import_legacy()` saves/restores cwd around such imports [A]. This is the
  POC's existing, deliberate link to the target repository.
- **Legacy codec import chain — confirmed risk, audit AUD-005 [A].** The state path's byte-level
  module `_b4_lib` is not importable in isolation: at module level it does
  `import _bh_session as bh` and `import _b3_lib` (`_b4_lib.py:72-73`), and `_bh_session` at module
  level does `import reapy` + `from reapy import reascript_api as RPR` (:39-40),
  `sys.path.insert(0, r"C:\repos\reaper-reapy-mcp\src")` and **`os.chdir(r"C:\repos\reaper-reapy-mcp\src")`**
  (:49-50), `from reaper_controller import ReaperController` (:53), imports `vital_analysis`,
  `vital_dialog_guard`, `vital_render` (:55-57), and reads registry JSON files at import (:66-67).
  So importing `_b4_lib` — even only for its pure framing functions — **transitively imports the
  legacy host stack and changes the process working directory** (`_env.import_legacy` restores the
  cwd afterwards, which mitigates the chdir leak inside the POC, but the imports themselves are
  real). V3 must not import `_b4_lib` from Core or from an ostensibly isolated provider; a dedicated
  codec-isolation task is scheduled before any Vital provider work ([08 §6](08-provider-and-host-interfaces.md), [14 P7](14-implementation-plan.md)).

## 16. Tests and evidence infrastructure

[A: tests/ inventory; no pytest anywhere under `C:\REAPER\Scripts\`]
- 32 `test_*.py` scripts + ~65 probes/dry-runs; **no pytest, no conftest, no runner**; each script is
  standalone with `main()` returning an exit code and a local `check(name, ok, detail)` helper.
- Layer convention: `test_l1_pure.py` (no REAPER), `test_l3_live_read.py` (reads only),
  `test_l4_live_write.py` (**self-restoring for the document and routes**: session snapshot before,
  restore + route-predicate verification after every case; refuses to run a write suite against an
  unresponsive plugin, exit code 2).
- **Scope of the L4 evidence — correction (audit AUD-015) [A].** L4's `snapshot()` captures a full
  control vector — `"controls": {n: c.value for n, c in a.inspect_patch().all_controls.items()}`
  (`test_l4_live_write.py:109`) — but its `restore()` applies the **document and routes only**
  (`patch_document` with a routes predicate; :118-141); the captured control vector is **never
  applied and never compared after restore**. The suite therefore supports the claim *"document and
  route changes are restored and the route list is verified"*; it does **not** establish restoration
  of the live parameter vector. V3 L4 tests must restore and verify every declared captured
  channel, parameters included ([12 §7](12-testing-strategy.md)).
- Live gating is by **script selection and CLI flags** (`--live`), not pytest markers [A].
- Shared helpers: `probe_common.py` (path bootstrap, dialog watcher prelude, `logs_dir()`, and the
  two-writer rule: `write_json` = stable snapshot vs `write_run_json` = unique per-run name),
  `envelope_fixture.py`, `morphology_fixtures.py` [A].
- Artifacts: `C:\REAPER\prompts\vital_sound_design\logs\` (`phase3_*.json`, `phase4g5_*.json`,
  per-run `<kind>__<UTC>Z__<hash>` naming; `capability_cache/cap__*.json`) [A][C].
- Contract gates over recorded evidence: e.g. `test_integrity.py` corrupts corpus validators and
  requires the sandbox to reproduce the pinned baseline exactly; `test_activation_4g4.py` etc. pin
  the arithmetic the live runs produced [A].
- **Caution recorded:** several probes *write* their logs when run; running them without a live host
  replaces recorded PASS evidence with environment failures. Phase A ran **no** tests [A/process].

## 17. Canonical architecture documents of the POC

`C:\REAPER\prompts\vital_sound_design\architecture\` [A]:
`FINAL_ARCHITECTURE.md` (**authoritative; supersedes the Phase-1 documents listed in its §16**),
`DECISIONS.md` (DEC-01…DEC-26), `PHASE_3_IMPLEMENTATION_PLAN.md`, and the Phase-1 set
(`ARCHITECTURE.md`, `RESPONSIBILITIES.md`, `BRIDGE_INTERFACE.md`, `MCP_INTERFACE.md`, `DATA_FLOW.md`,
`VITAL_MODEL.md`) which each carry "PHASE 1 — DESIGN ONLY" headers.

Key decisions relevant to V3 (from `DECISIONS.md`; classification added here) [A]:
- **Universal-mechanics decisions:** DEC-02 shared library/MCP+CLI consumers; DEC-03 no `reapy`
  outside bridge (test-enforced); DEC-05 raw params never the interface; DEC-14 dependencies
  enforced in the plan phase; DEC-16 batch-first; DEC-18 CLI is a consumer; DEC-19 snapshot
  best-effort **with manifest**; DEC-21 reuse proven code in place; DEC-22 **success = verified
  read-back, never API return; nothing swallows an exception**; DEC-26 the semantic layer resolves
  declarations, never words.
- **Vital-specific:** DEC-04 (vocabulary ban instance), DEC-07/08/09/10/12/15/17/23/25 (routes,
  slot identity, document write authority, two value spaces, correspondence, destination/source
  validation, one write order, audibility, line_mapping).
- **REAPER-host:** DEC-06 (render/transport/analysis stay in REAPER), DEC-11 (identity = host +
  plugin + addressing + fingerprint; never FX GUID).
- **Recorded corrections:** DEC-23's stereo-audibility claim was measured **wrong** (it compared a
  state against itself) and corrected; DEC-25's corpus counts were declared **stale** (a directory
  walk broke after 138 files) and replaced by a full census; FINAL §16 lists 8 differences from the
  Phase-1 contract [A]. These are evidence of the repository's measuring discipline and are cited in
  [12](12-testing-strategy.md) as the standard V3 tests must meet.

## 18. Vital-specific vs reusable — summary (full matrix in [03](03-component-responsibility-matrix.md))

| Vital-specific (must not become universal) | Universal mechanics (candidate Core) |
|---|---|
| 64-slot route table; slot identity; document write authority for the 7 route fields | Transaction: snapshot → apply → verify → commit, rollback verified per **captured channel**, named gaps |
| Per-control laws `affine/enum/exponential/identity/none`; corpus ranges; 775/775 correspondence | Two-space value model with **declared read/write authority per artifact** |
| "Never read a control value out of the document" (a Vital persistence property) | Typed errors; no swallowing; refusal taxonomy |
| Document-before-parameters order (Vital snapshot semantics) | Provider-declared write order, enforced by the engine |
| Vital locator (name/class hex/module/version), base64-JSON doc codec, ident format | Host Bridge: locate/identify/read/write/state transport + call timeout/wedge/dialog handling |
| Depth fitting numbers; source/destination vocabularies; DISABLE set rules | Dependency findings model (hard/soft/incompatible/warning) + pre-write refusal |
| Wavetable selection mechanics; line_mapping behavior | Capability profile + cache keyed by identity + digest + format |
| Measured constants (0.4 s settle, one-route-per-write, 680×, 22.2 s render) | "Success = verified read-back"; per-operation outcome honesty |

## 19. Open questions about the POC (unresolved in Phase A)

1. `capabilities.py` docstring says "twelve capability groups"; the literal contains 7 [D].
2. `build()` returns `"findings": []` and nothing inside `build` fills it — vestigial or intended
   for a different caller path [D].
3. Where `StateMismatch` / `ReadbackMismatch` are actually raised (presumed in `bridge/state.py`
   and/or `parameters.py`) [D].
4. Whether `restore_state`'s `settle_s` parameter is consumed anywhere [D].
5. `fxchain` is not exported from `bridge/__init__.py` [D] — deliberate or omission.
6. `_env.import_legacy` pulls Phase-2 modules (`_b4_lib`, `vital_dialog_guard`, `_bh_session`) whose
   current hygiene is outside this audit; V3 must treat them as **dependency-risk** ([15 R-03](15-risks-and-open-questions.md)).
7. **State-block targeting on multi-VST tracks.** As implemented, the decode/rebuild path selects
   the first `<VST>` block, not the selected instance ([§10](#10-statedocument-channel-and-the-codec-location));
   the failure mode when a non-Vital plugin occupies the first block (hard read error vs garbage
   decode) depends on that plugin's payload and was **not measured** [D]. V3 treats correct
   targeting as a new P0 contract (AUD-001), not as an inherited property.
8. **Can an identical-build replacement in the same slot be detected with available host evidence?**
   The POC records the FX GUID as a raw process pointer and never uses it as identity [A]; whether
   `TrackFX_GetFXGUID` changes when an FX slot is re-populated within a session was **not measured**
   [D]. This is a Phase-E investigation task feeding the V3 target-token contract
   ([05 §5a](05-universal-core-contract.md), [14 P3](14-implementation-plan.md)).
9. **Whether a full-parameter-vector capture is cheap enough** to be MANDATORY for every live
   write in V3 ([11 §2.3](11-planning-transactions-and-verification.md)). The POC measures batch
   parameter IO as one in-process hop [A][C], which makes it feasible, but the sweep cost is
   **not recorded** in the Phase-A evidence; P5/P8 must measure it before the snapshot contract is
   finalized.
