# 05 — Universal Core Contract

Status: **[D] proposed** (rev. 2026-10-10, Phase C remediation → Phase D consolidation → Phase E approvals and handoff — see [18](18-remediation-register.md), [19](19-owner-decisions.md), [20](20-implementation-handoff.md)).
This document defines the domain objects, interfaces and semantics the
Universal Core must implement. Implementation is Phase-E only, after approval.

## 1. Principles

1. **Declarations, not guesses.** The Core resolves what the profile/provider declares. Unknown
   names are refusals with suggestions, never fallbacks (POC DEC-26 / fail-closed).
2. **Two spaces, declared authority.** Every writable/readable field declares which channel is
   authoritative (host parameter array vs plugin state document) and how values convert.
   The POC's *"never read a control value out of the document"* is a **Vital instance** of this rule,
   not the rule itself ([02 §18]) [A].
3. **Batching across a slow boundary.** The LLM-facing and host-facing contracts are batch-first;
   there is no single-control host verb on the semantic path (POC DEC-16).
4. **No invented identities.** Route/field identity comes from the provider (Vital: slot index).
   Core never mints ids that a caller could hold past their validity (POC DEC-08 rationale).
5. **Success = verified read-back.** Core's transaction returns committed state, not host replies.
6. **Complete capture or refusal (Phase C, AUD-002).** Before the first write, V1 snapshots **every
   mutable channel the operation can change** and refuses the mutation outright when a complete,
   verifiable snapshot cannot be obtained. A named gap is a *diagnostic for reads* — it is never an
   excuse to proceed with a write whose effects cannot be restored.
7. **Wrong-target write prohibition (Phase C, AUD-001).** No state read, write or verification may
   address a block other than the one uniquely correlated to the validated target instance; an
   ambiguous correlation is a refusal before any mutation.

## 2. Domain objects

All objects are JSON-serializable dicts at the tool boundary; Python dataclasses internally.
Field tables below are **normative for V1**.

### 2.1 `PluginInstanceRef`

| Field | Type | Req | Meaning |
|---|---|---|---|
| `host` | string | ✔ | host adapter id, V1: `"reaper"` |
| `location` | object | ✔ | host-scoped location: `{"track_index": int, "fx_index": int}` for reaper |
| `provider` | string | ✔ | provider id, e.g. `"vital"` |
| `identity` | object | ✔ | provider fingerprint: `{fx_name, class_id, module, param_count, version}` (provider-defined keys) |
| `display` | string | – | human string, e.g. `"Vital (VST3i) @ track 1 fx 2"` |

Rules: `location` is **not** an identity — it is a lookup hint. `identity` here is the **profile
fingerprint** ([§5](05-universal-core-contract.md)): it decides
cache compatibility and *helps* decide whether the host still holds the intended plugin build — it
does **not** by itself prove the instance is the same one (Phase C, AUD-003). The fingerprint is
re-checked at use (`compare-on-use`), re-resolved once, then `StaleInstance` (POC `locate.validate`).
No GUID-like opaque pointer is ever used as durable identity (POC DEC-11) [A]; session-scoped host
handle evidence may be used for token revalidation but is never serialized ([§5](05-universal-core-contract.md)).
Resolvers return exactly one ref or raise `InstanceNotFound`/`AmbiguousInstance` — Core never picks
among equals.

### 2.2 `ControlCapability`

| Field | Type | Req | Meaning |
|---|---|---|---|
| `id` | string | ✔ | semantic identity (provider namespace), e.g. `filter_1_cutoff` |
| `label` | string | ✔ | display label |
| `module` | string | – | module id this control belongs to |
| `type` | enum | ✔ | `number` \| `enum` \| `bool` \| `state_ref` |
| `unit` | string | – | e.g. `Hz`, `%`, `semitones`, `%`-scale flag |
| `range` | object | – | `{min, max}` in **native/display** space (not normalized) |
| `step` | number | – | minimal meaningful increment, if declared |
| `enum_values` | array | – | for `type=enum`: `[{id, label}]` |
| `aliases` | array | – | accepted alternative names (provider-declared) |
| `authority` | object | ✔ | `{"read": "parameter"\|"state", "write": "parameter"\|"state"}` |
| `binding` | object | ✔ | provider/native metadata, opaque to Core: `{index, ident, state_key, ...}` |
| `law` | object | – | conversion declaration: `{kind: affine\|enum_index\|exponential\|identity\|none, params...}` |
| `evidence` | object | – | `{strength: strong\|weak\|none, source}` — provenance for measured laws |
| `discrete` | bool | – | no interpolation (POC enum write rule) |

Core treats `binding` and `law.params` as **opaque provider data**; it only dispatches
(`readable`, `writable` are derived: a control with `authority.read` and a binding is readable).

### 2.3 `ModuleDescriptor`

| Field | Type | Req | Meaning |
|---|---|---|---|
| `id` | string | ✔ | stable module id, e.g. `filter_1` |
| `kind` | string | ✔ | provider-defined kind (`oscillator`, `filter`, `operator`, ...) — **never assumed by Core** |
| `label` | string | – | display |
| `parent` | string | – | parent module id for hierarchy/graph |
| `controls` | array | – | ids (or omitted; controls carry `module`) |
| `state_refs` | array | – | state paths owned by the module (provider-defined) |
| `props` | object | – | provider extension namespace |

Core imposes **no taxonomy**: modules may be flat, hierarchical, or absent entirely.

### 2.4 Values

```
Value = {type, display, native, normalized?, unit?}
```
- `display` is the human/LLM-facing value (`22`, `"On"`, `-6.0 dB`).
- `native` is the provider-space value (Vital: document space).
- `normalized` is the 0..1 host-parameter value, present only when the channel is a parameter.
- Writes accept **display-space values** by default; raw normalized writes exist only behind an
  explicit provider-declared escape (out of V1's tool surface).

### 2.5 `RouteRef` / `RouteSpec`

| Field | Type | Req | Meaning |
|---|---|---|---|
| identity | provider-defined | ✔ | Vital: `slot: int` (1..64); no synthetic ids |
| `source` | string | ✔ (spec) | provider vocabulary member |
| `destination` | string | ✔ (spec) | provider vocabulary member |
| `depth` | Value | – | **semantic** depth in destination's unit |
| `amount` | number | – | native amount (provider space), used internally |
| `fields` | object | – | provider extras (Vital: `bipolar, stereo, power, bypass, line_mapping_supported`) |

Core's route contract is **operation-shaped**, not field-shaped:
`list / create / update / remove / depth`, with every mutation flowing through the provider codec
(state channel) and verified by read-back. Depth conversion is a provider hook
(`convert_depth(destination, depth) -> native_amount | refusal`) — Core never applies a universal
formula (POC: per-destination fitted factors, 246/316 accepted, honest refusals) [A].

### 2.6 `Finding` (dependencies, warnings, info)

```
{kind: "hard" | "soft" | "incompatible" | "warning" | "info",
 rule: string, subject: {control|route|module}, message: string, detail: {...},
 informational: bool?}
```
`hard` + not informational → pre-write refusal (`DependencyViolation` / `IncompatibleSettings`
equivalent). `soft`/`warning`/`info` ride in output as data (POC `dependencies.check_plan` /
`raise_on`) [A]. Rows this V1 keeps from the POC model: `disable_set` for structural transitions is
a **provider plan output**, not a Core concept.

### 2.7 `Plan`

```
{label, instance_ref, profile_key, changes: [PlanItem...], disable_set: {...},
 warnings: [...], findings: [...], write_order: ["state","parameters","verify"],
 n_slots_in_use?: int}
PlanItem = {subject, channel: "state"|"parameter", path|binding, from, to, value_space}
```
Core invariants on a plan: (a) every item names a channel; (b) the plan is built against the **live**
state (routes, flags, parameter values) and reports the table it resolved against; (c) a plan is
data — building it performs zero writes (POC `plan.build`: "Nothing is written").

### 2.8 `Outcome` — the post-write / recovery report (Phase C, AUD-007)

The outcome is **separate from the error**: a failed post-mutation call carries both a typed `error`
and the complete outcome ([06 §2](06-mcp-tool-contracts.md)).

| Field | Type | Meaning |
|---|---|---|
| `stage` | enum | `preflight\|plan\|snapshot\|state_write\|parameter_write\|verify\|commit\|rollback` — where the operation ended |
| `verdict` | enum | `ALL_VERIFIED\|REFUSED_BEFORE_ANY_WRITE\|REFUSED_AFTER_A_WRITE\|FAILED_AND_RESTORED\|FAILED_AND_PARTIALLY_RESTORED\|FAILED_AND_NOT_RESTORED\|ROLLBACK_FAILED\|NO_WRITES` (dry-run) |
| `final_state` | enum | `committed\|unchanged_verified\|partially_restored\|unknown` — mapping table in [11 §3](11-planning-transactions-and-verification.md) |
| `rollback` | object | `{attempted, verified, channels: {state: bool, parameters: bool}, why}` — an **attempted** rollback is not a **verified** one |
| `channels` | object | per-channel reports (`state`, `parameters`) with rows and mismatches |
| `host` | object | `{reads, write_calls_attempted, write_effects_verified, write_effects_unknown}` — see accounting rules below |
| `named_gaps` | array | diagnostics; in V1 an entry here accompanies a **refusal**, never a permitted write (principle 6) - with **one contract-defined exception** (2026-10-10): a capture that deliberately excludes a provider-declared plugin-derived leaf names that exclusion (`derived_leaf_not_restored`) on every path where a restore ran, including one that restored everything else successfully. The same fact has exactly two representations: `derived_excluded` is the **mechanism** (which leaves the restore set leaves out) and the named gap is the **consequence**. `rollback.verified` (the writes landed) and `rollback.complete` (the set covered the whole capture) are reported separately |

**Host accounting rules (normative):**
- `write_calls_attempted` counts every write verb invoked on the host. A refusal is reported as
  "zero host writes" **only** when this counter is 0.
- `write_effects_verified` counts write calls whose effects a later read-back verified beyond doubt.
- `write_effects_unknown` counts write calls after which the effect could not be established (wedge,
  connection loss, cancellation, unverifiable read-back). A failed commit does **not** imply zero
  host writes; an attempted rollback is **not** equivalent to a verified rollback.
- `host_reads` may be non-zero on a refusal (preflight-after-reads), which is why it is reported
  separately from writes.

### 2.9 Transaction, host and state objects (Phase C, AUD-004)

Seven retained concepts — the minimum coherent set; redundant candidate abstractions
(`HostState`, `Snapshot`, `StateChange`, `FxView`, `VerificationPolicy` as loose references) were
folded into these or given one-line definitions. Each has exactly one owner.

| Object | Owner (produced by) | Minimum data | Invariants | Tests |
|---|---|---|---|---|
| `FxHostFacts` | HostBridge | track index + track identity evidence (name; GUID only as *session evidence*, never serialized), fx_index, fx_name, class_id, module, param_count, version?, fx-slot count, session handle evidence? | read fresh for every validation; never treated as durable identity | mock-host conformance; replacement/movement fixtures (B-38…B-43) |
| `TargetedStateSlice` | HostBridge (locate), StateCodec (encode) | `chunk_digest` it was derived from, `line_start`/`line_end` (exact block span), `header_line`, `class_hex`, ordinal among matching blocks, `block_text` (the standalone block), provider framing parsed **from the block text** | exactly **one** block may match the target identity or the operation refuses; the span is only valid for its `chunk_digest`; a stale slice is re-located, never reused | multi-block fixtures (B-26…B-31); splice byte-identity of untouched blocks |
| `StateRevision` | Core | digest over (document bytes, route view, captured parameter vector), scope, read timestamp | advances only from verified reads; a transaction's own verified write advances it from its **own** read-back (no self-conflict); a mismatch against the expected revision is a conflict | revision advance tests; external-change conflict tests (B-42, B-51) |
| `StateSnapshot` | Core (codec supplies document/routes parts; bridge supplies parameter vector) | document snapshot, routes snapshot, `{ident → normalized}` parameter vector for the declared capture scope, base `StateRevision`, `manifest {captured, not_captured}` | must cover `provider.capture_scope()` completely or the mutation is **refused before the first write**; memory-only during a transaction | capture-completeness tests (B-32…B-34) |
| `StateWriteUnit` | StateCodec (inside `StateWritePlan`) | `order_index`, `kind` (`state`\|`parameters`), payload (document change set, or `[(ident, normalized)]`), expected revision, target token id, declared preconditions (e.g. "one route slot") | units are ordered; the engine executes them in order, revalidating target + revision before each; a unit never spans two blocks or two instances | order tests; per-unit revalidation tests (B-38…B-43) |
| `StateWritePlan` | StateCodec | ordered `units[]`, rollback plan (reverse units: restore document → restore parameters), verification plan (comparators + tolerances from the provider policy) | replaces the former `structural_units()` count: the codec must *decompose* multi-write structural limits explicitly (Vital: one route per state unit) | two mock codecs with different batching (L2 suite) |
| `WriteAttempt` | HostBridge (transport result) | `unit_index`, `attempted_utc`, `returned` (host value — recorded, never trusted), slice digest sent, read-back slice digest, `effects_verified`?, `error?` | one WriteAttempt per write verb invocation; feeds the `outcome.host` counters; `returned` is never evidence of effect | accounting tests (B-45/B-46); wedge/cancellation fixtures |
| `VerificationReport` | StateCodec (per unit), Core (aggregate) | `ok`, per-channel rows, `mismatched[]`, comparators + tolerances used, the **space** compared in | comparison happens in the space that was written (display requests re-resolved through the write's own conversion); a report without a comparator for a written value type is invalid | space-correctness regression (POC defect fixture); tolerance tests |

`VerificationPolicy` (provider-declared): `{tolerances per value type, comparators per value type,
settle/retry timing profile}`. `DocumentView` = provider-shaped decoded document (Vital: the JSON
dict). `DocumentSnapshot`/route snapshot = codec-produced, restorable-through-changes pieces of a
`StateSnapshot`.

## 3. Interfaces (protocol sketches — names final at implementation)

```python
class PluginProvider(Protocol):
    id: str                                        # "vital"
    implementation_revision: str                   # cache determinant (AUD-008)
    def artifact_paths(self) -> list[str]: ...     # files hashed into the profile cache key
    def matches(self, facts: FxHostFacts) -> bool: ...
    def identify(self, facts: FxHostFacts) -> dict: ...           # profile fingerprint fields
    def build_profile(self, identity: dict) -> CapabilityProfile: ...
    def normalize_request(self, profile, request) -> ResolvedRequest | Refusal: ...
    def convert_depth(self, profile, destination: str, depth) -> float | Refusal: ...
    def dependencies(self) -> DependencyRuleSet: ...              # rules as data + evaluator hooks
    def codec(self) -> StateCodec: ...
    def write_order(self) -> tuple[str, ...]: ...                 # e.g. ("state","parameters")
    def verification_policy(self) -> VerificationPolicy: ...      # tolerances/comparators/timing
    def capture_scope(self) -> CaptureScope: ...                  # mutable channels an op may touch

# Optional hooks Core recognizes when a provider declares them (not part of the protocol,
# because Core's callers must work without them):
#   profile_source_rows(identity) -> list[{"file","sha256"}]   # cache-key material without a build
#   adopt_profile(profile) -> None                              # take the served profile
#   state_view(document) -> dict                                # P30: read-only descriptor view,
#       attached to `plugin_state` data as `state_view`. It answers content a generic summary
#       cannot express (for the first provider: which table each oscillator reads, by content
#       digest and frame count). A provider without it answers exactly what it did before; a
#       failure inside it is reported as a warning row, never as an error on a readable state.

class StateCodec(Protocol):
    def decode(self, slice: TargetedStateSlice) -> DocumentView: ...
        # operates ON THE SELECTED SLICE, never on an undifferentiated track chunk (AUD-001)
    def plan_state_writes(self, doc: DocumentView, changes: dict) -> StateWritePlan: ...
        # ordered StateWriteUnits + rollback + verification plan (replaces structural_units())
    def encode_slice(self, doc: DocumentView, unit: StateWriteUnit,
                     current: TargetedStateSlice) -> TargetedStateSlice: ...
        # new block text for exactly the selected span; byte-exact outside the document
    def verify(self, readback: DocumentView, unit: StateWriteUnit,
               policy: VerificationPolicy) -> VerificationReport: ...
    def snapshot_document(self, doc: DocumentView) -> DocumentSnapshot: ...
    def restore_changes(self, doc: DocumentView, snap: DocumentSnapshot) -> dict: ...
        # restoration expressed as document changes, executed through plan_state_writes

class HostBridge(Protocol):
    def list_tracks(self) -> list[TrackView]: ...
    def fx_facts(self, track_index: int, fx_index: int) -> FxHostFacts: ...
    def param_idents(self, ref, indices: list[int]) -> list[str | None]: ...
    def read_params(self, ref, indices: list[int]) -> list[float]: ...   # one batched hop
    def write_params(self, ref, pairs: list[tuple[str, float]]) -> WriteAttempt: ...
        # (ident, normalized); returns host value + read-back for the engine's verification
    def read_state_chunk(self, ref) -> str: ...
    def locate_state_slice(self, ref, facts: FxHostFacts, chunk: str) -> TargetedStateSlice: ...
        # span + header + class + ordinal; uniqueness enforced; raises on ambiguity (AUD-001)
    def write_state_slice(self, ref, slice: TargetedStateSlice, new_block: TargetedStateSlice,
                          *, timing: TimingProfile) -> WriteAttempt: ...
        # exact transport: splice ONLY the slice span into the chunk,
        # SetTrackStateChunk(whole chunk), settle, re-read. NO semantic callbacks: the bridge
        # owns transport, never provider verification (AUD-004)
    def re_read_slice(self, ref, facts: FxHostFacts, unit: StateWriteUnit) -> TargetedStateSlice: ...
    def call(self, fn, *args, timeout: float, label: str): ...    # timeout/wedge discipline

class CapabilityCache(Protocol):
    def key_for(self, fingerprint: dict, *, provider_id: str, provider_revision: str,
                artifact_digest: str, source_digest: str,
                profile_schema_version: int) -> str: ...          # AUD-008: all determinants
    def load(self, key: str) -> CacheEntry | None: ...             # key/hash/format mismatch ⇒ refuse
    def save(self, key: str, profile) -> CacheEntry: ...
        # validates any existing file's identity first; foreign/mismatched file ⇒
        # quarantine by atomic rename, then atomic temp+replace write (AUD-012)
    def status(self) -> dict: ...                                  # refusals + reasons
```

## 4. Semantics of the value/authority model (normative)

1. A **control** value is read from its declared `authority.read` channel only.
2. A **write** goes to `authority.write`; Core never silently redirects a write to another channel.
3. If a requested control has `type=state_ref`, it is a **structural** operation: it must go through
   `codec.plan_changes` (POC wavetable precedent [A]).
4. Enum writes are discrete: no interpolation; invalid members are refusals listing legal values.
5. `law.kind = none|unmeasured` ⇒ any semantic conversion request for that control is refused with
   the provider's reason — **never approximated** (POC DEC-10).
6. Verification compares **in the space that was written**: display requests are re-resolved through
   the same conversion the write used before comparison (POC recorded defect [A: `_verify_batch` docstring]).
7. Unit traps are first-class: a `%`-unit control receiving `1.0` is refused/flagged per provider
   declaration (POC `percentage_units` rule precedent [A]).
8. **Snapshot-or-refuse.** A mutation proceeds only with a `StateSnapshot` covering
   `provider.capture_scope()` completely and verifiably; otherwise the transaction refuses
   **before the first write** ([11 §2.3](11-planning-transactions-and-verification.md)).
9. **Restoration order is provider-declared and verified per channel.** For Vital V1 the declared
   order is: restore document → restore captured parameter values → verify document → verify every
   restored parameter (a document write resets ordinary parameters, so parameters must be rewritten
   **after** the document, and the document must be re-verified **after** nothing else writes it).
10. **Target revalidation.** Before each state-write unit and each parameter batch, the target token
    is revalidated and the expected `StateRevision` is checked; a mismatch refuses (or rolls back,
    mid-sequence) — never a silent re-target, never a write to a replacement instance ([§5](05-universal-core-contract.md)).

## 5. Identity model: profile fingerprint × target token × state revision (Phase C, AUD-003)

Three separate concepts; conflating any two of them is a defect:

| Concept | Question it answers | Where it lives | Lifetime | Used for |
|---|---|---|---|---|
| **Profile fingerprint** | “Which plugin/profile/version mapping is this?” | `PluginInstanceRef.identity`, profile `identity` block | durable (cacheable data) | capability-cache compatibility; provider matching; a *hint* that the host still shows the intended build |
| **Write-target token** | “Is this still the exact instance I resolved and validated?” | server-side session record; an **opaque id** returned to callers | one server session; revalidated at use | binding a request/transaction to an intended instance; refusal on stale/mismatched token |
| **State revision** | “Is the mutable state still what I read when I planned?” | computed by Core from fresh reads | advances on every verified read/write | conflict detection between plan/units/rollback; never part of the token |

**Profile fingerprint** fields (provider-declared; Vital): `fx_name`, `class_id`, `module`,
`param_count`, `version?`. It does **not** prove same-instance: an identical build placed in the
same slot matches every field.

**Write-target token** rules:
- Opaque to callers; maps server-side to: project/session context, `FxHostFacts` at resolution
  (including session-scoped handle evidence *if measured reliable*), the provider fingerprint, and
  the resolved location. **A raw host pointer/GUID is never serialized** as durable user identity.
- Revalidated immediately before every state-write unit and parameter batch (and before rollback):
  fresh `host.fx_facts(...)` compared against the token's facts.
- Stale or mismatched ⇒ typed `TargetChanged` refusal; the transaction **never** silently re-resolves
  onto a new instance. If exactly one candidate exists, the refusal carries it for the caller to
  re-issue explicitly.
- **Known limitation (must stay documented):** whether an identical-build replacement in the same
  slot can be distinguished at all depends on host evidence (`TrackFX_GetFXGUID` behavior when a
  slot is re-populated is *unmeasured* — [02 §19.8](02-vital-poc-audit.md)). Phase P3 measures it;
  where the evidence cannot distinguish, the contract **fails closed**: the operation refuses when
  the distinguishing evidence is absent or ambiguous, and the limitation is reported in the
  refusal. No guarantee is claimed that cannot be established.
- **Measured, 2026-10-10 (P8 `p8_l3_identity__20261010T203101Z`):** the host does **not** distinguish
  it. Removing an FX and re-adding the same build at the same slot reproduced the same handle
  (`handle_changed: false`) while a different slot reported a different one, and the build facts
  (class id, module, name, parameter count) came back identical. The artifact records **no datum the
  host exposes** that separates the replacement from the original - and it records no *ordinal or
  state-digest comparison of the replacement*; the digests it does carry
  (`original_block_digest`/`original_vector_digest` with `original_block_unchanged: true`) are there
  to show the addressed instance never moved, which is a different question. So the token's question
  ("is this still the exact instance I resolved?") **cannot be answered** for an identical-build
  replacement.
- **Consequence, binding since the owner's safety-gate correction (2026-10-11):** a
  **caller-supplied token is refused for any mutation** (`IdentityContinuityUnprovable`, before any
  host work), because accepting it would let the caller's belief stand in for verification - the
  same class of defect as an accepted `force`. The remedy is stated in the refusal: address the
  instance **by location** (or by the object `plugin_instances` returns in `data.instances[]`),
  which is a present-tense claim the layer verifies inside the call (complete capture, per-unit
  revalidation, write, verified read-back, verified restore). Reads keep accepting the token,
  because a read makes no write claim; a token whose facts disagree with the host is still
  `TargetChanged`.
- The token must **not** depend on a mutable state digest in a way that invalidates it after the
  transaction's own successful writes; token = instance identity, revision = state.

**State revision** rules:
- Captured at plan time; checked before each sequential state-write unit and before rollback; after
  every **verified** write the expected revision advances from that write's own read-back (the
  transaction cannot conflict with itself).
- An unexpected change (external writer, user edit, another process) ⇒ `StateConflict` before
  mutation; detected mid-sequence ⇒ rollback of the transaction's own writes.
- **Residual limitation (accepted in [19 Decision 3](19-owner-decisions.md), AUD-010):** the host API
  provides no compare-and-swap, so revision comparison is **best-effort conflict detection**, not
  atomic exclusion; a race window between check and write remains ([11 §7](11-planning-transactions-and-verification.md)).
  Rollback therefore restores only channels whose revision still matches and leaves an externally
  changed channel untouched, reported as `not_restored (external_change)` ([11 §2.7](11-planning-transactions-and-verification.md)).

**Selector semantics** (how a caller picks an instance): `{track_index, fx_index}` (exact),
`{track_name}`, `{name}` (substring); ambiguity without a selector raises. Resolution is cached per
(host, location) within a session, invalidated on fingerprint mismatch. A stale ref never silently
retargets; the caller gets `StaleInstance` plus the single new candidate ref (if any) so it can decide.

## 6. Validation rules (Core, before any host access)

| Rule | Behaviour |
|---|---|
| Unknown control/module/route name | refusal + suggestions (provider `suggest`) |
| Value out of declared range | refusal with range |
| Unit/space mismatch (e.g. % trap) | refusal/flag per provider declaration |
| Enum member unknown | refusal listing legal values |
| Control not writable / not readable | refusal naming the capability |
| Route vocabulary violation | provider policy refusal; **V1 has no override** - `force=true` is itself refused (`UnsupportedOverride`), so a caller is never told a policy was overridden when it was not (owner Decision 4, 2026-10-10) |
| State path not rooted at a declared key | refusal (POC `DOCUMENT_TOP_KEYS` guard [A]) |
| Missing required arg | tool-level refusal, no dispatch (POC server `SPEC.required` [A]) |

## 7. Error taxonomy (V1 list)

Ported from POC `errors.py` [A], renamed into three families for the merged repo:

**Transport / host (raised by HostBridge):** `NotConnected`, `Wedged`, `Unreadable`,
`HostContractUnavailable` (the POC's `BindingUnavailable`, renamed).

**Domain / resolution (raised by Core/Provider):** `InstanceNotFound`, `AmbiguousInstance`,
`StaleInstance`, `WrongPlugin`, `VersionMismatch`, `UnsupportedControl`, `InvalidValue`,
`NoConversion`, `InvalidRoute`, `InvalidSourceFamily` (policy), `UnsupportedOverride` (an
override/escape argument V1 does not implement - the request is refused before any host work),
`DependencyViolation`, `IncompatibleSettings`, `PlanError`, `UnsupportedProvider` (no applicable
provider configured), `TargetSliceAmbiguous` (AUD-001: state block cannot be uniquely correlated
to the target).

**Conflict / identity (Phase C, AUD-003/AUD-010):** `TargetChanged` (token stale or mismatched —
never a silent re-target), `IdentityContinuityUnprovable` (a mutation rested on instance continuity
the host cannot prove — the token-carrying case above; refused before any host work),
`StateConflict` (expected revision disagrees with the live state),
`SnapshotUnavailable` (complete capture impossible ⇒ mutation refused before first write).

**Write outcomes (raised/reported by Transaction):** `WriteNotApplied`, `StateMismatch`,
`ReadbackMismatch`, `WriteOrderViolation`, `RollbackFailed`, `RestoreIncomplete`.

All exceptions serialize as `{code, message, detail}` inside the envelope's `error` object; the
plugin tool layer converts them to `ok:false` results carrying **both** the typed error and the
`outcome` block ([§2.8](05-universal-core-contract.md)) — never prose
inside a success payload, never a bare error without the recovery state.

## 8. Required tests for this contract

Mapping to [12](12-testing-strategy.md) (each is a Phase-E deliverable):

- Unit: every domain object's validation; law dispatch incl. `none` refusals; `%`-trap refusal;
  enum discreteness; route identity from provider; finding severity → refusal mapping; the §2.9
  object invariants each have a fixture (slice uniqueness, revision advance, snapshot completeness).
- Contract: `PluginProvider`/`StateCodec`/`HostBridge` conformance suites run against the mock
  implementations **and** (read-only parts) the REAPER bridge; **two mock codecs with different
  state shapes/batching** run the codec suite (AUD-004).
- Boundary: unknown names; out-of-range; wrong authority; ambiguous instance; stale fingerprint;
  stale/mismatched target token; revision conflict; snapshot unavailable ⇒ 0 writes; wedged host
  call; unreadable value; corrupt cache; foreign-key cache (byte-preserved); partial cache file;
  state-path not rooted; verification-in-wrong-space regression fixture; ambiguous state block ⇒ 0
  writes; untouched blocks byte-identical.
- Transaction state machine: every verdict (8, including `NO_WRITES`) reachable and distinguished;
  `final_state` mapping holds; rollback verification per channel; `ROLLBACK_FAILED` loud path;
  `FAILED_AND_RESTORED` never reported while any captured channel is unverified.
- Envelope: schema-stable output for ok/fail/dry; `write_calls_attempted == 0` (not just
  "zero writes") asserted on refusals; every terminal outcome combination covered ([11 §3](11-planning-transactions-and-verification.md)).
