# 08 — Provider and Host Interfaces

Status: **[D] proposed** (rev. 2026-10-10, Phase C remediation → Phase D consolidation → Phase E approvals and handoff — see [18](18-remediation-register.md), [19](19-owner-decisions.md), [20](20-implementation-handoff.md)).
Defines the five distinct concepts the brief requires to stay separate:
Universal Core, Provider, Capability Profile, State Codec, Host Bridge (+ REAPER implementation).

## 1. Concept map

```
Universal Core        semantic mechanics; no plugin names, no reapy
   ├── Capability Profile   static declarations (07) — produced BY a provider
   ├── Provider             plugin-specific semantics, bindings, laws, rules, codec
   │      └── State Codec   document/state semantics for that plugin
   └── Host Bridge          host-level transport contract (no plugin semantics)
          └── REAPER Host Implementation   reapy/Reascript API
```

## 2. `PluginProvider` contract (V1, minimal)

| Method | Input | Output | Notes |
|---|---|---|---|
| `id` | – | `"vital"` | stable provider id used in refs/profile |
| `implementation_revision` | – | provider-declared revision string | **cache determinant** covering behaviour-affecting provider code (dependency rules, laws, codec, write order, verification policy, limits) — AUD-008 |
| `artifact_paths()` | – | list of files | hashed into the profile cache key (must cover every behaviour-affecting artifact the provider ships) — AUD-008 |
| `matches(facts)` | `FxHostFacts` | bool | e.g. fx_name contains "Vital" + class/module check |
| `identify(facts)` | `FxHostFacts` | identity dict | pure; no writes; produces the **profile fingerprint** |
| `build_profile(identity)` | identity | CapabilityProfile | from provider data; cached by Core |
| `normalize_request(profile, request)` | semantic request | ResolvedRequest or refusal list | resolves names/values; **no host access** |
| `convert_depth(profile, destination, depth)` | semantic depth | native amount or `NoConversion` | per-destination fit; honest refusals |
| `dependency_rules()` | – | ruleset (data + declared hooks) | evaluated by Core's engine |
| `codec()` | – | StateCodec | one per provider in V1 |
| `write_order()` | – | e.g. `("state","parameters")` | enforced by Core; not caller-configurable |
| `verification_policy()` | – | tolerances + comparators + timing | per value-type; the space rule applies |
| `capture_scope()` | – | `CaptureScope` | **the mutable channels an operation may touch** and must snapshot — Vital V1: document + routes + full parameter vector — AUD-002 |
| `limits()` | – | list[str] | honest restrictions surface |

**Provider may:** read its own data files, compute laws/groups/depth, validate vocabulary, decide
refusals, decode/encode its document format (inside its codec).
**Provider may not:** import `reapy` (only the HostBridge object it is given), know MCP, write host
state directly (all writes go through Core's transaction → HostBridge), or depend on another provider.

## 3. `StateCodec` contract (Phase C, AUD-001/AUD-004)

The codec operates on the **selected native payload**, never on an undifferentiated track chunk:
its `decode` input is a `TargetedStateSlice` ([05 §2.9](05-universal-core-contract.md)).

| Method | Semantics |
|---|---|
| `decode(slice)` | the slice's own block text → `DocumentView` (provider-shaped). Decoding the wrong block is impossible by construction: the slice was located by `locate_state_slice`, and its span/digest bind it to the chunk it came from. |
| `plan_state_writes(doc, changes)` | → `StateWritePlan`: **ordered `StateWriteUnit`s** + rollback plan + verification plan. Replaces the former `structural_units()` count — the codec must *decompose* multi-write structural limits explicitly (Vital: one route per state unit; non-route changes ride the first unit) |
| `encode_slice(doc, unit, current_slice)` | new **block text** for exactly the selected span; byte-exact outside the document; rooted-path enforcement (the POC refuses unrooted paths — a missing `settings.` root writes where the plugin never looks [A]) |
| `verify(readback_doc, unit, policy)` | compare **in the space that was written**; per-change problems; comparators + tolerances from the provider policy; returns a `VerificationReport` |
| `snapshot_document(doc)` / `restore_changes(doc, snap)` | document-level snapshot + restoration expressed as document changes (executed through `plan_state_writes` — never a blind blob write) |

The codec is where the *plugin's* document format and structural semantics live. The POC's
`bridge/state.py` mixes this with track-chunk transport; V3 splits it: transport + slice
locate/splice → HostBridge; codec → provider [A: 02 §10, 03 verdict]. **The codec must supply enough
information for Core to execute ordered, target-aware, revision-aware state operations and
recovery** (AUD-004).

## 4. `HostBridge` contract + REAPER implementation

Contract (see [05 §3] for the full protocol). V1 REAPER implementation notes, each with POC precedent:

1. **Lazy single connection** per process; typed `NotConnected` on failure; explicit `reconnect()`
   [A: POC `bridge/connection.py`]. (Process startup itself requires REAPER — pre-existing eager
   `reapy.connect()`; see [13 §4](13-existing-mcp-integration.md), AUD-006.)
2. **One chokepoint** for every host call with a **hard timeout** (daemon-thread execution),
   wedge classification (`Wedged`) and transport-error classification (`NotConnected`) —
   a hung host must never hang the MCP server [A: POC `bindings.call`].
3. **Batch parameter IO executed inside REAPER** (`reapy.inside_reaper`); ident-resolved inside the
   same hop (`TrackFX_GetParamFromIdent` → `SetParamNormalized`); read-back includes the ident
   [A: POC `parameters.py`]. The full-vector capture required by AUD-002 is one such batched read.
4. **No client-side param-index cache** — indices go stale; idents are the durable binding on VST3
   [A: POC design decision + rationale].
5. **Targeted state transport (AUD-001)**: `locate_state_slice` finds the exact block span whose
   class/ordinal correlation with the target facts is **unique** (else `TargetSliceAmbiguous`,
   zero writes); `write_state_slice` splices **only that span** into the chunk, calls
   `SetTrackStateChunk(whole chunk)`, settles, and re-reads; the setter's return value is recorded
   and never trusted. Untouched blocks remain byte-identical.
6. **The bridge owns transport, not semantics (AUD-004).** There is **no `verify` callback in the
   bridge**: no provider predicate, no plugin vocabulary. Settle/retry timing comes from the
   provider's declared `TimingProfile` via the engine; the bridge waits and re-reads; **Core decodes
   and verifies via the codec**.
7. **Dialog policy:** reapy calls are blocked by modal REAPER dialogs; the POC runs a Win32 watchdog
   that (a) always dismisses while a host call is in flight, (b) is conservative when idle and never
   dismisses question dialogs, (c) captures dialog text before dismissal and attaches it to failures
   [A]. V3 V1: implement the capture + conservative policy as **opt-in configuration** (default on
   for live tests, documented off-switch), never auto-answering user questions.
8. **Master track / FX addressing:** the bridge supports track indices plus `-1` master conventions
   as needed; FX position is resolved by host enumeration, identity by provider rules [A: 01 §4].
9. **Per-instance serialization** keyed to the validated **target token** (not a slot number), held
   from final resolution through the complete verified outcome and recovery path; a measured
   precedent shows writes ~0.35 s apart can fail without serialization [C]. Revision checks and the
   lock are *best-effort* conflict reduction — the host has no compare-and-swap (AUD-010).

## 5. What the Core must expose to providers (and what it must not)

Core hands providers exactly three things: (1) the **HostBridge contract object** (read/write
primitives only), (2) the **profile** it built (immutable), (3) the **live state views** the
transaction reads. Core keeps: planning, dependency evaluation orchestration, transaction state
machine, verification bookkeeping, cache, envelope, errors. A provider never sees a tool schema, a
FastMCP object, or the process environment beyond its own configuration.

## 6. Provider loading & placement (V1 decision)

**Decision (Phase-D recommended; owner approval pending — [19 Decision 2](19-owner-decisions.md)):**
the Vital provider is an **in-repo, dependency-isolated package** at
`src/reaper_reapy_mcp/plugins/providers/vital/` implementing the V3 contracts, importing **no**
`reapy`/`reaper_controller`/`_bh_session`/`_b4_lib` (AUD-005). The repository ships the *contracts*
plus a **mock provider** used by tests. Providers are enabled by id
(`REAPER_MCP_PLUGIN_PROVIDERS`); unset ⇒ zero providers enabled. The Vital registry data is read from
a configured directory — `REAPER_MCP_VITAL_REGISTRY_DIR`, default
`C:\REAPER\prompts\vital_sound_design\registry` — and is **not vendored** by default: it is generated
from measured evidence, so a copy inside the repo would be a second source of truth. Offline tests
use pinned fixture subsets, so the suite needs no `C:\REAPER`. The profile records
`provenance.source_digest` over the files actually read, so a stale or replaced directory is visible
and can never be served silently.

**Registry directory resolution, validation and error handling (specified for P7; owner-approved
boundary — [19 Decision 2](19-owner-decisions.md)).** The implementation must behave exactly as
follows; no silent fallback is permitted anywhere in this sequence.

1. **Resolution order.** (1) `REAPER_MCP_VITAL_REGISTRY_DIR` when set (even to an empty string — an
   empty value is a configuration error, not "unset"); (2) otherwise the documented default
   `C:\REAPER\prompts\vital_sound_design\registry`. The value is normalised to an absolute path
   (`os.path.abspath` + `os.path.normpath`); `~` and environment variables are **not** expanded (a
   literal path is required, so a typo cannot silently resolve somewhere else); relative values are
   resolved against the process working directory **and reported as such** in the profile provenance.
2. **Validation (at provider load / first use, before any read is served).** The path must exist and
   be a directory; it must contain the expected registry files (`controls`, `modules`, `laws`,
   `capabilities` — exact filenames fixed in P7 against the POC's `adapter/registry.py` [A]); each
   file must be readable, parse as JSON, and pass the provider's schema check ([07 §4](07-capability-profile-schema.md));
   the resolved directory and a digest over the files actually read are recorded in the profile
   provenance.
3. **Error handling (all fail closed, all echo the resolved path).** Variable set but invalid
   (missing/not a directory/unreadable/invalid JSON/failed schema check) ⇒ typed
   `IncompatibleSettings` naming the variable and the resolved path, the provider is **not** used for
   that call, and **no fallback to the default directory occurs**. Variable unset and the default
   directory unusable ⇒ the same typed error, naming the default. Provider not enabled at all
   (`REAPER_MCP_PLUGIN_PROVIDERS` unset or not listing `vital`) ⇒ typed `UnsupportedProvider`, and the
   registry directory is **not** touched (no disk access, no error if it is missing).
4. **Startup and test isolation.** Nothing in this sequence runs at import time or at server startup:
   resolution happens on first plugin-tool call that needs the provider, so a server with zero
   providers never touches the filesystem and the offline suite never needs `C:\REAPER` (pinned
   fixture subsets are used instead).
5. **Caching interaction.** The directory's content digest is part of the profile cache key
   ([09 §3](09-discovery-and-cache.md)); a changed directory therefore produces a different profile
   key rather than a silently reused profile.

**Codec isolation (AUD-005).** Importing the legacy `_b4_lib` chain is forbidden from Core and from
the provider: `_b4_lib` module-level imports `_bh_session`, which at module level imports `reapy`,
`ReaperController`, `vital_analysis`, `vital_dialog_guard`, `vital_render`, reads registry JSON and
executes `os.chdir(r"C:\repos\reaper-reapy-mcp\src")` ([02 §15](02-vital-poc-audit.md), verification
[17 AV-05](17-planning-evidence-index.md)). **Source verification fixes the extraction boundary
([17 AV-13](17-planning-evidence-index.md)):** inside `_b4_lib.py` only `_get_chunk`, `_set_chunk` and
`_default_ctrl` (plus the module-level aliases) touch the host; the byte codec and routing helpers
import nothing. P7 therefore ports exactly that stdlib-only set into the provider's codec:

| Approach | Pros | Cons |
|---|---|---|
| **(a) Extract the verified host-free function set** — `_b64d`, `find_vst_block`, `parse_framing`, `decode_document`, `encode_item_b`, `encode_item_a`, `wrap_lines`, `rebuild_chunk`, `_dig`, `list_routings`, `find_free_slot`, `make_routing`, `clear_routing`, `export_document` ([17 AV-13](17-planning-evidence-index.md)) — into the provider's dependency-free `codec.py`, validated against the POC's byte-identity fixtures | keeps the proven byte-exact algorithm in one place; the host-touching functions are excluded by measurement, not by guesswork; fixtures already exist | extraction work at P7; must be re-validated against the fixtures; every ported function reviewed for hidden globals |
| **(b) Clean provider-owned codec** re-implementing the verified algorithm from the documented layout + fixture corpus (fallback if a ported function proves not host-free in practice) | fully isolated by construction; no legacy lineage at all | re-implementation risk; the algorithm is fine-grained (length fields, 280-char wrap, trailing artifact) and every detail must be re-proved against fixtures |

Either way, acceptance is the same and is **testable**: importing the provider in a clean process
must not import `reapy`, `ReaperController`, `_bh_session` or `_b4_lib`, must not change the working
directory, and a **recursive import-boundary test** enforces it ([12 §2](12-testing-strategy.md),
V-12). [19 Decision 2](19-owner-decisions.md) records approach (a) as the recommended position with
(b) as the fallback: the POC's own rule is "reuse proven code in place; extract rather than
rewrite", and the byte-exactness of this algorithm was hard-won and fixture-verified, while (b)
duplicates the highest-risk code in the system.

Rationale for in-repo isolated placement:
- The provider is code, not data: keeping it in the repo makes it reviewable, versioned and testable
  against the contracts it implements, and the isolation is enforced mechanically (V-12), so the
  repository does not become plugin-coupled by accident.
- The Vital-specific parts that must **not** live in Core stay where they are: registry data and
  measured laws remain in the POC data tree ([02 §15](02-vital-poc-audit.md)) and are read at runtime
  through the configured directory.
- No duplication of Vital logic in two places (the POC itself rejected "two implementations" as a
  recorded failure mode: `resolve()` exists twice with a live `IndexError` [A: POC server.py docstring]).
- Core stays plugin-agnostic: the provider package is the only place Vital identifiers appear, and
  the import-boundary test bans them from Core.

Alternatives considered: (a) out-of-tree provider (the Phase-A position at
`C:\REAPER\Scripts\sound_design\vital_provider\`) — superseded: the provider's runtime and tests
would live outside the reviewed repository; (b) vendoring the registry JSON into the repo —
acceptable if the owner wants full self-containment, but it creates a second copy of generated data
that needs a refresh story (offline tests already use pinned fixtures, so this is optional);
(c) entry-point discovery via packaging — deferred (the launch is
`py -3.11 -c …` from source; entry points add packaging requirements incompatible with the current
run-from-source model [A: 01 §2]); (d) HTTP/IPC provider — rejected (latency, new failure surface;
the POC's measured batch advantage exists precisely because IO runs in-process [C]).

## 7. Vital provider mapping (contract element → POC source)

| V3 contract element | POC source (host-free functions ported at P7; nothing imported) |
|---|---|
| `matches/identify` | `bridge/locate.py` (name substring, class hex, module string, param_count, version gate) |
| `build_profile` | `adapter/registry.py` + `controls.Control.from_row` + `spaces/laws.py` + `capabilities.py` groups/depth + module tables in `model.py` |
| `normalize_request` | `adapter/llm.py` `request_preflight` + `_resolve_*` helpers + `plan.resolve_control_name` |
| `convert_depth` | `capabilities.depth_fit` / `modulation_depth` |
| `dependency_rules` | `adapter/dependencies.py` predicates + `DISABLE_CANDIDATES` |
| `codec` | document semantics from `bridge/state.py` + `adapter/modulations.py` plan/verify/snapshot + `adapter/wavetables.py` structural ops — **algorithm taken from the legacy code, but the code itself is never imported** (AUD-005); targeting rebuilt slice-exact (AUD-001) |
| `capture_scope` | the POC's observed state: document + routes snapshotted, parameters *not* (AUD-002 gap) — V3 declares the full parameter vector as capture scope |
| `write_order` | fixed document→parameters (POC DEC-17) |
| `verification_policy` | `transaction._verify` tolerances (1e-5 state / 1e-3 parameter) + executor's 1e-6 restore check |
| `limits` | VST2 unsupported; one route per state write; no line_mapping writes; growth-refusing wavetable policy |

## 8. Boundary enforcement (tests, not prose)

Per the POC's proven discipline (its boundary test mechanically bans `reapy` imports outside
`bridge/` [A]). **Scope: the `plugins/` package only** — the repository's existing host modules
already import `reapy` and are out of scope (AUD-013; the Phase-A repo-wide phrasing was wrong).

1. `plugins/` core modules (model, profile, provider, resolve, values, dependencies, plan,
   transaction, cache, tools) may not import `reapy`/`reascript_api` and may not contain provider
   identifiers (`vital`, `osc_`, `filter_1` — including comments, to keep docstrings honest).
2. `plugins/host/reaper.py` is the only module **inside `plugins/`** importing `reapy`.
3. The Vital provider (`plugins/providers/vital/`, in-repo — [19 Decision 2](19-owner-decisions.md))
   may import `reapy` **not at all**; a **recursive import-boundary
   test** asserts that importing the provider in a fresh process pulls in none of
   `reapy`/`ReaperController`/`_bh_session`/`_b4_lib` and leaves the working directory unchanged
   (AUD-005 acceptance).
4. Provider loading is the only place a provider module name appears in the repo, and it is
   configuration-driven; registration of the 7 tools is unconditional (AUD-013).
5. The provider package is the only location where Vital identifiers may appear; `plugins/` Core
   modules and `tests/plugins/` core fixtures stay plugin-agnostic (L0 test, AUD-013).
