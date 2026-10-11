# 06 — MCP Tool Contracts

Status: **[D] proposed** (rev. 2026-10-10, Phase C remediation → Phase D consolidation → Phase E approvals and handoff — see [18](18-remediation-register.md), [19](19-owner-decisions.md), [20](20-implementation-handoff.md)).
Names follow the existing repo's snake_case convention; all are
prefixed `plugin_` to avoid collision with the existing 57 tools [A: 01 §5]. Registration happens on
the existing FastMCP server (`Reaper Control`) via a new `setup_plugin_tools(mcp, ...)` called from
`create_server()` [A: 01 §8]. **Registration is unconditional** (AUD-013/AUD-014 Q-05): with no
applicable provider configured, every plugin tool returns a typed `UnsupportedProvider` result —
registration never depends on provider-discovery side effects.

## 1. The surface, and why it is 7 tools

| Tool | One-line responsibility | POC precedent |
|---|---|---|
| `plugin_instances` | list/resolve plugin instances (candidates + identity + target token) | `inspect_vital_instance` + `bridge.locate` |
| `plugin_describe` | identity + module summary + capability counts (cache-backed) | `plugin_describe` |
| `plugin_find` | search controls/modules; detail for one module/control | `plugin_find`, `plugin_module`, `capability_group(s)`, `control_capability` |
| `plugin_read` | targeted, batched current-value reads | `plugin_read`, `inspect_section` |
| `plugin_write` | batch-first verified write; **dry-run by default, explicit apply** | `plugin_write_batch`, `apply_request`, `apply_*` |
| `plugin_state` | state/document **inspect** (snapshot/restore deferred from V1) | `inspect_patch` (+ POC `snapshot_state`/`restore_state` as the deferred precedent) |
| `plugin_modulation` | route ops: list, create, update, remove, depth | `inspect_modulation`, `create/update/remove_modulation`, `modulation_depth` |

### 1.1 Mutation classification (normative — AUD-009)

Every operation is classified, and the classification is consistent across all mutators:

| Tool / op | Class | Applies changes? |
|---|---|---|
| `plugin_instances`, `plugin_describe`, `plugin_find`, `plugin_read`, `plugin_state(inspect)` | **read-only** | no |
| `plugin_write(dry_run=true)` — **the default** | **dry-run/preview** | no |
| `plugin_write(dry_run=false)` | **mutating** | yes, via the transaction |
| `plugin_modulation(list/depth)` | read-only | no |
| `plugin_modulation(create/update/remove, dry_run=true)` — **the default** | dry-run/preview | no |
| `plugin_modulation(create/update/remove, dry_run=false)` | **mutating** | yes, via the transaction |
| *(deferred)* `plugin_state(snapshot/restore)` | **filesystem-mutating / mutating** | not in V1 |

**Uniform policy (AUD-009/AUD-014 Q-01): no mutating call applies changes unless the caller
explicitly requests application (`dry_run: false`).** This reverses the Phase-A batch-first default
(`dry_run=false`, POC precedent) — the audit's fail-safe position wins; the POC's rationale (internal
preflight makes the dangerous case impossible) remains recorded in [02 §4](02-vital-poc-audit.md) as
the trade-off being declined. The extra round-trip cost is accepted.

**Alternatives evaluated (brief §16):**

| Option | Verdict | Reason |
|---|---|---|
| Keep the POC's 24-tool surface 1:1 | Rejected | includes six mutually redundant escape hatches (`apply_patch_changes`, `apply_parameter_changes`, `apply_state_changes`, `apply_request`, `request_preflight`, `plugin_write_batch`) and 4 capability tools the cache makes redundant; the brief demands a minimal surface |
| Separate `plugin_capabilities` tool | Rejected | `plugin_describe` (summary) + `plugin_find` (detail, incl. `group`) cover it; a third consumer surface duplicates cache reads |
| Separate `plugin_module` tool | Folded into `plugin_find` (`group=` returns the module's controls) | same data path |
| Separate `plugin_preflight` tool | Rejected | preflight is `plugin_write(dry_run=true)`; a separate tool invites "prefire then write" races |
| Raw parameter escape hatch (`set/get_raw`) | Rejected for V1 | POC DEC-05: raw parameters are the implementation layer, never the interface; keeps semantic enforcement honest |
| A CLI mirror (`vit`-style) | Out of V1 | MCP-first (brief §17); CLI can return later as a *test harness*, not a product |

## 2. Envelope (shared by all 7 tools) — schema 2 (Phase C, AUD-007)

Heir of the POC's envelope [A: `vital/mcp/results.py`], but the **error is separate from the
post-write/recovery outcome** (schema 1 could not carry a failed post-mutation call's state).

Success:

```json
{ "tool": "plugin_write", "schema": 2, "ok": true,
  "data": { ... }, "warnings": [],
  "outcome": { "stage": "commit", "verdict": "ALL_VERIFIED", "final_state": "committed",
               "rollback": {"attempted": false, "verified": false, "channels": {}},
               "channels": {"state": {...}, "parameters": {...}},
               "host": {"reads": 2, "write_calls_attempted": 2,
                        "write_effects_verified": 2, "write_effects_unknown": 0},
               "named_gaps": [] },
  "meta": { "instance": {"host":"reaper","location":{...},"provider":"vital","identity":{...}},
            "target_token": "tok_…", "profile_key": "vital:1b2c…:pmap-v1" } }
```

Failure (both fields present when a mutation was attempted):

```json
{ "tool": "plugin_write", "schema": 2, "ok": false,
  "error": {"code": "WriteNotApplied", "message": "...", "detail": {...}},
  "warnings": [],
  "outcome": { "stage": "verify", "verdict": "FAILED_AND_RESTORED",
               "final_state": "unchanged_verified",
               "rollback": {"attempted": true, "verified": true,
                            "channels": {"state": true, "parameters": true}},
               "channels": {...},
               "host": {"reads": 4, "write_calls_attempted": 3,
                        "write_effects_verified": 1, "write_effects_unknown": 0},
               "named_gaps": [] } }
```

Rules: a typed error is **never** text inside a success payload [A: POC FINAL §15.2; target-repo
defect class in 01 §4]; "zero host writes" is claimed only when `outcome.host.write_calls_attempted
== 0`; a failed commit never implies zero host writes; an attempted rollback is never equated with a
verified one ([05 §2.8](05-universal-core-contract.md)). `plugin_write`/`plugin_modulation` return
`outcome` always; read-only tools return the same envelope with `outcome.verdict` omitted or
`NO_WRITES`-equivalent read semantics — the schema positions are fixed once, in [05 §2.8](05-universal-core-contract.md).

## 3. `plugin_instances`

- **Purpose:** enumerate/select plugin instances so every other tool has a valid ref.
- **Input:** `{ "provider": "vital"?, "selector": {"track_index": int, "fx_index": int}?
  | {"track_name": str} | {"name": str}?, "include_identity": bool = true }` (`name` = substring match; selectors follow [05 §5](05-universal-core-contract.md))
- **Output data:** `{ "instances": [ PluginInstanceRef ], "ambiguous": bool, "refused": [...] }`
  — an empty list is a valid, useful answer ("not found" ≠ server error).
- **Errors:** `NotConnected` (host unreachable), `HostContractUnavailable` (host contract probe failed).
- **Side effects:** none. **Verification:** fingerprint fields come from live host reads; resolving an
  instance also issues its session-scoped **target token** ([05 §5](05-universal-core-contract.md), AUD-003).
- **Tests:** zero/one/many candidates; selector narrows; ambiguity surfaced without guessing;
  target token issued and revalidatable; **startup lifecycle (AUD-006):** the process requires REAPER
  at start (eager `reapy.connect()`); if the connection is lost *after* startup, a call returns a
  typed `NotConnected` — no claim is made about a server that never started ([13 §4](13-existing-mcp-integration.md)).
- **Example:** `{}` → `{"ok":true,"data":{"instances":[{"host":"reaper","location":{"track_index":0,"fx_index":0},
  "provider":"vital","identity":{"fx_name":"VST3i: Vital (Vital Audio)","param_count":2858,"module":"vital.vst3",
  "version":"1.5.5"},"display":"Vital (VST3i) @ track 1 fx 1"}],"ambiguous":false},
  "meta":{"target_token":"tok_9f21…"}}`

## 4. `plugin_describe`

- **Purpose:** identity + module topology + capability summary; the first call after instances.
- **Input:** `{ "instance": PluginInstanceRef | location-selector, "detail": "summary"|"full" = "summary" }`
- **Output data:** `{ "identity": {...}, "profile": {"key": str, "schema": int, "source": "cache"|"built",
  "version": str}, "modules": [ModuleDescriptor], "counts": {"controls": n, "writable": n, "enums": n,
  "state_only": n, "no_law": n}, "routes": {"supported": bool, "identity": "slot", "slots": 64},
  "warnings": [...] }` (`detail=full` adds per-module control lists).
- **Errors:** `InstanceNotFound`, `AmbiguousInstance`, `StaleInstance`, `VersionMismatch`,
  `UnsupportedProvider`.
- **Side effects:** may write one cache file — **validate-identity → quarantine foreign/mismatched
  file → atomic temp+replace**; a foreign file's bytes are preserved (AUD-012, [09 §4-§5](09-discovery-and-cache.md));
  the profile key includes the provider implementation revision + artifact digest (AUD-008).
- **Verification:** profile validated against schema + identity before use; a cache hit re-checks key,
  content hash, counts and the provider-revision determinants.
- **Tests:** cache miss→build→save; hit; stale digest→rebuild; corrupt/foreign file→quarantine
  (byte-compared) + rebuild; counts match profile; `no_law` count surfaced (not hidden); key changes
  when any behaviour-affecting determinant changes. A missing **required** identity field fails
  closed: no writes/mutations/restore, no validated-profile caching — diagnostic reads are permitted
  but marked provisional (AUD-008).
- **Example (data excerpt):** `{"counts":{"controls":775,"writable":761,"state_only":14,"no_law":3},
  "routes":{"supported":true,"identity":"slot","slots":64}}`

## 5. `plugin_find`

- **Purpose:** locate controls/modules by name or group; answers "what can I ask about X".
- **Input:** `{ "instance": ..., "query": str?, "group": str?, "kind": "control"|"module"|"route_vocab" = "control",
  "limit": int = 12, "include_capability": bool = false }`
- **Output data:** `{ "matches": [ {kind, id, label, module, type, unit, range?, enum_values?, aliases?,
  authority, evidence} ], "suggestions": [...], "groups": [...] }`
- **Errors:** `UnsupportedControl` only when `group` unknown (a query with no matches is `ok:true`,
  `matches: []`).
- **Side effects:** none — served from the capability profile/cache, **no host read** [A: POC
  capability tools answer "without a host call"].
- **Tests:** name/state-key/substring/alias matching; group filter; limit; suggestions ordering;
  no host reads asserted (mock host counts calls).

## 6. `plugin_read`

- **Purpose:** targeted current values, batched.
- **Input:** `{ "instance": ..., "controls": [str, ...], "include_display": bool = true }`
- **Output data:** `{ "values": { "<control>": {"display": ..., "native": ..., "normalized": ?,
  "unit": ?} }, "problems": [ {control, code, message, detail} ] }`
- **Errors:** per-control problems are data (partial success is explicit); whole-call errors only for
  instance/transport failures.
- **Side effects:** none (read-only).
- **Verification semantics:** values are read from the declared read authority; display derived via
  provider law/scale, never re-read from a formatter unless the profile says so [A: 02 §6].
- **Tests:** batch produces one host read for N controls (mock counts); unknown control → problem row
  not exception; state-only control read from state channel; unreadable value reported, never defaulted.
- **Example:** `{"controls":["filter_1_cutoff","filter_1_switch"]}` →
  `{"values":{"filter_1_cutoff":{"display":24.0,"native":24.0,"normalized":0.1203,"unit":"semitones"},
  "filter_1_switch":{"display":"On","native":1.0,"normalized":1.0}}}`

## 7. `plugin_write` — the core tool

- **Purpose:** one call: preflight → (live reads) → plan → transaction → apply → verify → commit,
  or a dry-run preview. Batch-first by design [A: POC `plugin_write_batch` rationale; DEC-16].
- **Input:**
```json
{ "instance": {...},
  "request": {
    "label": "optional human label",
    "controls": { "filter_1_cutoff": 24, "filter_1_switch": "On" },
    "structural": false,
    "routes_create": [ {"source":"lfo_1","destination":"filter_1_cutoff","depth":12} ],
    "routes_update": [ {"slot": 3, "depth": -6} ],
    "routes_remove": [ {"slot": 7} ],
    "state": { "some.provider.path": value }        // provider-declared state fields only
  },
  "dry_run": true }
```
> **Note (2026-10-11, owner safety-gate correction).** `routes_create` (and a `routes_update` that
> names a source/destination) is **refused** for a provider whose `route_into_off_filter` rule cannot
> be evaluated - Vital in V1 - with `DependencyViolation` / `detail.unevaluable`, identically for
> `dry_run` true and false ([10 §2.3a](10-state-modulation-dependencies.md)). The example above shows
> the request **shape** the contract defines, not an operation that is currently applicable.
- **`dry_run` default: `true` — explicit application required (AUD-009/AUD-014 Q-01).** A caller that
  wants changes applied passes `dry_run: false`; there is no `settle_s` or other caller-controlled
  timing (AUD-009 removes it; timings are provider-declared and engine-supplied).
- **Output data (committed, `dry_run=false`):** `{ "committed": true, "plan_summary": {...},
  "route_plan": { "routes_create": [...], "resolved_slots": {...} }, "read_cost_next": {"needed": 0,
  "why": "this response already contains the read-back of everything it changed"},
  "outcome": { "stage": "commit", "verdict": "ALL_VERIFIED", "final_state": "committed",
  "rollback": {"attempted": false}, "channels": {"state": {"rows": [...], "mismatched": []},
  "parameters": {"rows": [...]}}, "host": {...}, "named_gaps": [] } }`
- **Output data (refused):** `{ "committed": false, "problems": [typed rows with legal values/ranges],
  "resolved": {...}, "outcome": { "verdict": "REFUSED_BEFORE_ANY_WRITE", "final_state": "unchanged_verified",
  "host": {"write_calls_attempted": 0} } }`
- **Output data (dry-run):** `{ "ok": bool(would_write), "committed": false, "dry_run": true,
  "plan_summary": {...}, "dependency_findings": [...], "preview": {...}, "outcome": {"verdict": "NO_WRITES"} }`
- **Errors (as envelope `error`, always with `outcome`):** `DependencyViolation`, `IncompatibleSettings`,
  `InvalidValue`, `UnsupportedControl`, `NoConversion`, `SnapshotUnavailable`, `TargetChanged`,
  `StateConflict`, `TargetSliceAmbiguous`, `VersionMismatch` (AUD-014 Q-03: **refused for unmeasured
  versions — no V1 override**), `WriteNotApplied`, `WriteOrderViolation`, `RollbackFailed`,
  `RestoreIncomplete`, transport errors.
- **Side effects:** plugin state and parameter writes — only when `dry_run=false`; one cache read at
  most; **zero write calls** on refusal (`outcome.host.write_calls_attempted == 0`, counter-proven in tests).
- **Verification semantics:** every written item is read back **in the space it was written** and
  compared with provider-declared tolerances; success requires all items verified; rollback is itself
  verified per captured channel and reported ([11 §2.7](11-planning-transactions-and-verification.md)).
- **Snapshot semantics:** before the first write the transaction assembles a complete capture of
  document + routes + the parameter capture scope; an incomplete capture refuses the mutation
  (AUD-002; [11 §2.3](11-planning-transactions-and-verification.md)).
- **Tests (the largest matrix):** dry-run default writes nothing (every tool-level test asserts the
  default); refusal has `write_calls_attempted: 0`; every verdict reachable (8) and every
  verdict↔final_state mapping holds; per-item reject rows carry channel; space-mismatch regression
  fixture (the POC's three-false-mismatch defect [A]); multi-unit sequencing; capture-failure ⇒ 0
  writes; target/revision revalidation per unit; rollback per-channel verification; untouched blocks
  byte-identical; `read_cost_next.needed == 0` after commit.
- **Example (excerpt, committed):** `{"committed":true,"outcome":{"verdict":"ALL_VERIFIED",
  "final_state":"committed","channels":{"parameters":{"rows":[{"control":"filter_1_cutoff",
  "written":24,"read_back":0.1203,"display":"24.0","agrees":true,"channel":"parameter"}]}},
  "host":{"write_calls_attempted":2,"write_effects_verified":2,"write_effects_unknown":0}},
  "plan_summary":{"n_controls":2,"n_routes_create":1,"n_disable_set":0}}`

## 8. `plugin_state` — V1 scope: inspect only (AUD-009)

- **Purpose:** state/document **inspection**: provider-declared structural summary, structural-key
  inventory, targeted decode of the selected slice.
- **Input:** `{ "instance": ..., "op": "inspect", "include": ["routes","wavetables",...]? }`
- **Output data:** `{ "state": {provider-declared summary}, "structural_keys": [...], "counts": {...},
  "slice": {"header": ..., "class_hex": ..., "ordinal": n, "is_first_vst": bool} }`
- **Errors:** `Unreadable` (state undecodable/plugin unsupported), `TargetSliceAmbiguous`,
  `UnsupportedProvider`, `InvalidValue`.
- **Side effects:** none.
- **Deferred from V1 (AUD-009/AUD-014 Q-09):** persistent `snapshot`/`restore` (arbitrary-path
  artifact create/restore) is **not exposed**. Transactional snapshots are in-memory and internal to
  `plugin_write`. If ever reintroduced, the full audit requirements apply: allowed storage root, path
  normalization + symlink policy, atomic creation, overwrite refusal, provenance, immutable manifest,
  content hash, provider/profile compatibility, instance-target validation, preflight, transaction,
  outcome reporting — and any restore must use the same target-validation and transactional-recovery
  contract as other writes ([10 §1.3](10-state-modulation-dependencies.md)).
- **Tests:** inspect on a multi-block chunk reports the **selected** slice correctly (AUD-001);
  unknown/unreadable state is a typed refusal, never a silent empty summary; the tool list contains
  no snapshot/restore ops (deferral is test-asserted against the registration table).
- **Example:** `{"op":"inspect"}` → `{"state":{"settings_keys":781,"routes":64,"active_routes":3},
  "structural_keys":["modulations","wavetables","sample","custom_warps","lfos","random_values"],
  "slice":{"header":"<VST \"VST3i: Vital (Vital Audio)\" ...>","class_hex":"56535456697461766974616C00000000",
  "ordinal":0,"is_first_vst":true}}`

## 9. `plugin_modulation`

- **Purpose:** route lifecycle + semantic-depth conversion.
- **Input:** `{ "instance": ..., "op": "list"|"create"|"update"|"remove"|"depth",
  "routes": [...]?, "selector": {"slot": int} | {"source": str, "destination": str}?,
  "destination": str?, "depth": number?, "force": bool = false, "dry_run": bool = true }`
- **Mutation identity (AUD-009):** `update`/`remove` must name the route identity (`slot`) or a
  selector that resolves to **exactly one** route. An ambiguous selector is a typed refusal for
  mutations; the deterministic "first match + report the rest" behaviour is permitted for
  **list/preview only** — a mutation never silently picks a target. `create` has no identity to name;
  the plan picks a free slot and **reports the resolved slot** in the result [A].
- **`dry_run` default: `true`** for `create`/`update`/`remove` (same explicit-apply policy as
  `plugin_write`).
- **Output data:** `list` → `{ "routes": [RouteRef...], "n_slots_total": 64, "n_free": n }`;
  `create` → per-route outcome incl. **resolved slot**; `update/remove` → matched slot + before/after;
  `depth` → `{ "native_amount": x, "law": {...}, "evidence": ... }` or a typed refusal (`NoConversion`)
  naming the destination as non-affine **without inventing a factor** [A: 246/316 precedent].
- **Errors:** `InvalidRoute`, `InvalidSourceFamily` (policy). **`force` is declared and refused:**
  V1 implements no override, so `force=true` answers `UnsupportedOverride` before any host work and a
  wrong type for the argument is `InvalidValue` - an argument that looks like a permission is never
  accepted while nothing honours it (owner Decision 4, 2026-10-10);
- **Route creation is refused while the rule is unevaluable** (owner safety-gate correction,
  2026-10-11): `create` (and any `update` that would name a `source`/`destination`) answers
  `DependencyViolation` with `detail.unevaluable: ["route_into_off_filter"]` and
  `write_calls_attempted: 0`, identically for `dry_run: true` and `false` - a preview must not present
  an unverifiable operation as applicable. `list`, `depth`, `remove` and non-pair `update`s are
  unaffected (see [10 §2.3a](10-state-modulation-dependencies.md));
- **A caller-supplied `target_token` is refused for a mutation** (`IdentityContinuityUnprovable`,
  before any host work): a token claims instance continuity, and the measured host cannot prove it
  ([05 §5](05-universal-core-contract.md)). Reads keep accepting the token; `plugin_instances`
  returns the instance object (`data.instances[]`) with `location` + `identity` for addressing;
  `InstanceNotFound`, `TargetChanged`, `StateConflict`, transaction outcomes for mutations.
- **Side effects:** create/update/remove write through the state codec + transaction — only with
  `dry_run: false`; zero write calls otherwise.
- **Verification:** routes read back from the live table through the **selected slice** (state channel
  is write authority for route fields); `create` reports the actual landed slot; multi-route requests
  execute as sequential verified units (Vital: one route per state write).
- **Tests:** create picks a free slot and reports it; ambiguous selector **refused for mutations** and
  listed for reads; source-family policy refusal + `force` path; depth refusal list matches provider
  (Vital: 70/316 destinations refuse); remove of an empty/None slot is typed, not silent; default
  dry-run proven; per-unit token/revision revalidation.
- **Example (shape only - this request is now REFUSED, see the route-creation bullet above):**
  `{"op":"create","routes":[{"source":"lfo_1","destination":"filter_1_cutoff","depth":12}],
  "dry_run":false}` → the *shape* a successful create used to answer,
  `{"committed":true,"route_plan":{"routes_create":[{"resolved_slot":4,"source":"lfo_1",
  "destination":"filter_1_cutoff","amount":0.1836,"depth":12}]},"outcome":{"verdict":"ALL_VERIFIED"}}`.
  Since 2026-10-11 the answer to this call is `DependencyViolation` /
  `REFUSED_BEFORE_ANY_WRITE` with `write_calls_attempted: 0`, for `dry_run` true and false alike;
  the `committed: true` block is kept only as the documented envelope shape. What `remove`/`update`
  answer today is the same envelope with `data.committed` and `outcome.verdict`/`final_state`
  (`tests/plugins/test_p12_safety_gates.py::test_the_registered_modulation_tool_still_lists_and_removes`
  pins a committed removal at `ALL_VERIFIED` and one `write_state_slice`).

## 10. Registration & compatibility

- **Registration:** `setup_plugin_tools(mcp: FastMCP, *, config)` called from `create_server()`
  immediately after `setup_mcp_tools(...)` [A: `server.py`]. Uses the proven SPEC-driven closure
  pattern: each tool validates required args, dispatches through a `DISPATCH` table, returns the
  envelope [A: POC `mcp/server.py`]. **Unconditional** (AUD-013): the 7 tools always register (when
  the server process starts, per the lifecycle below); no applicable provider ⇒ typed
  `UnsupportedProvider` result from any call — never dependent on discovery side effects, never
  conditional on configuration.
- **Names:** `plugin_*` — no collision with the 57 existing tools [A].
- **Transport:** stdio, already supported; no new process, no new client registration.
- **Startup lifecycle (AUD-006, Option A — [19 Decision 4](19-owner-decisions.md)):** the **process requires REAPER at startup**
  because `BaseController.__init__` calls `reapy.connect()` eagerly and raises on failure [A: 01 §3].
  The plugin layer itself does no host work at import/registration time, and if the connection is lost
  after startup, the affected call returns a typed `NotConnected` (detectable: reapy raises; the
  bridge classifies — POC precedent). **No promise is made that a tool answers when REAPER was never
  running**; changing that requires Option B (lazy-startup redesign), explicitly out of V1 scope — it
  re-plans the phase sequence and is an owner scope change, not a task ([13 §4](13-existing-mcp-integration.md)).
- **Error compatibility:** plugin tools deliberately do **not** use the existing
  `{"status": "error", "message": str}` shape; the envelope (schema 2) is documented here and in
  [13](13-existing-mcp-integration.md) as a compatible addition (different tools, disjoint names).
- **Capability exposure to LLMs:** `plugin_describe`/`plugin_find` responses are designed as the
  *only* capability vocabulary an LLM needs; tools must not require the caller to know parameter
  indices, idents or document paths.
