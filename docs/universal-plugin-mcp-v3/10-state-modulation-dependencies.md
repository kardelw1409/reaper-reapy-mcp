# 10 — State, Modulation and Dependencies

Status: **[D] proposed** (rev. 2026-10-10, Phase C remediation → Phase D consolidation → Phase E approvals and handoff — see [18](18-remediation-register.md), [19](19-owner-decisions.md), [20](20-implementation-handoff.md)).
Covers three contracts the brief keeps together: the state/document
channel + codec boundary, the universal modulation model, and the dependency/validation mechanism.
All three are derived from measured POC behavior [A: 02] **as corrected by the Phase-C audit**
(AUD-001/AUD-002/AUD-009).

## 1. The state/document channel

### 1.1 Split of responsibilities

| Layer | Owns | Does not own |
|---|---|---|
| Host Bridge | reading/writing the host's state transport (REAPER: track chunk text; `GetTrackStateChunk` / `SetTrackStateChunk`), settle/retry timing, transport failure typing | any interpretation of the bytes |
| State Codec (provider) | decoding the plugin's document, rooted paths, change planning, byte-exact re-encode, structural ops, snapshot/restore content | host transport mechanics |
| Core | calling codec methods inside the transaction, recording outcomes, verification bookkeeping | document format knowledge |

### 1.2 Hard rules

1. **Slice-exact targeting (Phase C, AUD-001).** Every state read, write and verification addresses
   a `TargetedStateSlice` — the exact block span uniquely correlated to the validated target — never
   "the first VST block" and never an undifferentiated track chunk. The correlation is decided from
   host facts (class id, module, ordinal among matching FX/blocks) **and must be unique**; an
   ambiguous correlation raises `TargetSliceAmbiguous` with **zero writes**. Writes splice only the
   selected span; every untouched block remains byte-identical (test-enforced).
2. **`SetTrackStateChunk`'s return value is not evidence.** Verification is decode-and-compare of
   the **selected slice** after the write
   [A: POC state.write_document; "SetTrackStateChunk returns 1 even when nothing landed"].
3. **Every state change path must be rooted at a declared key.** Unrooted paths are refused:
   `modulations[0].source` (missing the `settings.` root) writes where the plugin never looks, which
   looks exactly like a write that did not land [A: POC `_set_path` guard].
4. **Settle and retry are declared timings, not sleeps in a loop.** POC: decode-verify, settle
   0.4 s, one retry at 2.0-3.0 s, wedge classification [A][C]. Timings come from the provider's
   declared timing profile — never caller-supplied ([06 §7](06-mcp-tool-contracts.md), AUD-009).
5. **Byte-exact re-encode where the format demands it.** Framing is provider data; the codec never
   re-serializes with a different shape if the plugin requires byte fidelity [A: POC `_b4_lib`
   framing, "reuse in place" verdict] — the algorithm is reused (extracted pure module) or
   faithfully re-implemented, but the legacy module is **never imported** (AUD-005).
6. **Structural operations are codec operations.** A structural change is a *transition*, not an
   assignment: the provider's `StateWritePlan` may include explicit companion changes (the POC's
   DISABLE set) computed, not requested [A: POC `dependencies.disable_set`].
7. **Multi-write structural limits are decomposed by the codec, not counted.** The codec returns an
   **ordered `StateWritePlan`** (replacing the former `structural_units()` count): Vital declares at
   most one modulation route per state write, so the transaction issues sequential verified units,
   re-reading the live slice between them [A][C: measured 8/8 sequential vs 0/2 batched]. Core
   revalidates the target token and expected revision **before each unit** ([05 §2.9](05-universal-core-contract.md)).

### 1.3 State op set (V1 surface)

V1 exposes **`inspect`** (targeted decode + summary + key inventory + structural-key listing), plus
provider-declared structural-op inspection (Vital V1: wavetable selection readable; growth refused
by policy). **Transactional snapshots live in memory only** — assembled inside the transaction
([11 §2.3](11-planning-transactions-and-verification.md)) — and **persistent create/restore of
restore-point files is deferred from V1** (AUD-009/Q-09: arbitrary-path snapshot/restore is a large
safety surface — file containment, overwrite, provenance, compatibility — that V1 does not need).
If a future phase reintroduces it, the requirements from the audit apply (allowed storage root, path
normalization/symlink policy, atomic creation, overwrite refusal, artifact provenance, immutable
manifest, content hash, provider/profile compatibility, instance-target validation, preflight,
transaction, outcome reporting). Unknown/unsupported state ops are typed refusals (`Unreadable`,
`InvalidValue`), never silent no-ops.

## 2. Modulation (universal model, provider implementation)

### 2.1 Model

- `RouteRef`: provider-identified (Vital: **slot**, 1..64; no invented ids), with `source`,
  `destination`, `depth` (semantic), `amount` (native), provider extras.
- Operations: `list`, `create`, `update`, `remove`, `depth` (conversion query).
- **Route fields are written through the state codec** for the provider that declares state as the
  write authority (Vital: all 7 fields; a parameter write alongside a routing write does not take)
  [A]. The *universal* rule is: profile declares `modulation.write_channel`; the engine respects it.
- Route/state writes respect the slice rule (§1.2.1): the transaction writes the route document
  through the selected slice only.
- Determinism for **reads/preview**: ambiguous selectors (source/destination matching multiple
  slots) act on the deterministic first match in provider order **and report the remaining matches
  as data** [A].
- **Mutations require explicit identity (Phase C, AUD-009):** an `update`/`remove` must name the
  route's identity (Vital: `slot`) or a selector that resolves to **exactly one** route; an
  ambiguous selector is a typed refusal for mutations — the "first match" behaviour is permitted
  for listing/preview only. Never silently select a mutation target.
- Route mutations are planned against the live table, are revalidated (token + revision) before the
  state unit executes, and are verified by reading the route back from the live table.

### 2.2 Semantic depth → native amount

- The conversion is a **provider hook** (`convert_depth`), never a universal formula [A: brief §12].
  Vital implements per-destination least-squares fits over measured display states:
  `amount = depth / native_per_unit` where `native_per_unit` is fitted; refusals where the display
  is not affine (worst residual > ½ display quantum) — 246/316 destinations accepted; the refused set
  is itself reported [C].
- A refusal is a first-class answer (`NoConversion` + reason), never an approximation.

### 2.3 Route vocabulary policy

- Providers may declare source/destination vocabularies and family rules (Vital: 31 sources,
  316 destinations, "no LFO → `modulation_N_amount`" from zero corpus precedent). The POC's `force`
  escape is **declared but not implemented in V1**: `force=true` answers `UnsupportedOverride` before
  any host work, so a policy refusal is never presented as overridable (owner Decision 4, 2026-10-10).

### 2.3a Dependency rules the provider declares but does not evaluate (2026-10-10, extended 2026-10-11)

A rule the provider cannot decide must never be presented as checked. In the profile and in every
mutation response the rules carry `evaluated: false` with the input each would need, and the response
names them (`dependency_rules_not_evaluated`), so an empty findings list is never read as "everything
was checked". Where a rule's **subject** is decidable and its deciding input is not, the subject is
**guarded**: a plan that enters it is refused before the first write with the rule and the missing
input named, and the refusal says *unevaluable* rather than *unmet*.

**Two guards are enforceable** (both derived from measured data, never hand-typed):

| Rule | Subject (decided) | Deciding input (missing) | Guard |
|---|---|---|---|
| `fm_needs_modulator` | a plan that changes an oscillator distortion **type** (spelled either way: the profile's measured `state_key`, or the DISABLE table's own name for it) | the other oscillator's level | refuse the subject |
| `route_into_off_filter` | a plan that **creates** a `(source, destination)` pair: `routes_create[*]`, `routes_update[*]` naming a non-empty `source`/`destination`, or a state write under `settings.modulations` that does not clear a pair field (the codec's own layout, parsed by the codec), **plus** a world that counts creates without carrying their specs | whether the destination is a filter input (the registry carries no destination families) and whether that filter is on (a live-document flag) | refuse the subject |

`wave_frame_needs_multiframe_wavetable` stays unguarded: the measured table itself declares it
informational, so refusing on it would harden the provider's own declaration. It is disclosed and
never fired (owner Decision 1; route guard added by the owner's safety-gate correction, Decision 6a).

**What the route guard deliberately does not refuse** (its safety provably does not depend on the
rule, so blocking it would be blocking for the appearance of completeness): `routes_remove[*]`,
`routes_update[*]` that carries no `source`/`destination` (an amount/power/curve change on an
existing pair), a state write that **clears** a pair field (an empty value removes a pairing), state
writes outside the route table (e.g. `settings.modulation_<n>_amount`), and every read-only
operation (`list`, `depth`, `plugin_state`, `plugin_read`, `plugin_find`, `plugin_describe`).

### 2.4 Known provider limit (Vital, V1)

`line_mapping` (per-route curve): the write channel strips curves on 1.5.5; the provider declares
`line_mapping_write_supported: false`, refuses requested curves, and warns that editing a
curve-carrying route will drop the curve [A: DEC-25]. V3 keeps this as a *declared limit*, not a
Core rule.

## 3. Dependencies and validation

### 3.1 Mechanism

- Rules are **provider data + provider-evaluated predicates**, surfaced as `Finding` rows
  ([05 §2.6]). Kinds: `hard` (pre-write refusal), `soft`/`warning`/`info` (ride in output).
- Evaluation happens in the **plan phase against the projected world**: live state overlaid with the
  planned values/routes. The POC's recorded reason: judging against the raw snapshot falsely refuses
  a correct patch because the snapshot's flags are stale [A].
- Ordering: `normalize → read live → build plan → evaluate findings → (raise on hard) → apply`
  [A: `Transaction.apply` calls `raise_on` before any write].

### 3.2 Rule content (Vital V1, examples that must survive the port)

| Rule | Kind | Meaning |
|---|---|---|
| FM/RM distortion needs a modulator oscillator | hard | otherwise bit-identical to off |
| DIRECT OUT makes downstream changes inaudible | hard, informational | refuses nothing; explains |
| route into an OFF filter is silent | hard | gated on actual routes |
| single-frame wavetable makes Wave Frame inert | soft | warning |
| percentage-unit trap (`Mix = 1.0` means 1 %) | hard | judged on the **requested** value |
| structural change requires the disable set | transition | plan adds computed off-values |

### 3.3 What V1 does *not* build

No dependency DSL, no rule expression language, no generic constraint solver. The POC's code
predicates + one data table are the complexity ceiling V1 copies.

## 4. Required tests

- State: slice-exact targeting on a multi-block mock chunk (multiple VST blocks; preceding foreign
  VST; two Vital instances; adjacent VST2/VST3 blocks where the fixture supports it) — the intended
  block changes and verifies, **untouched blocks stay byte-identical**, a mismatched correlation
  causes zero writes ([12 B-26…B-31](12-testing-strategy.md)); rooted-path refusal; decode-compare
  verification on the slice (API-return-ignored regression: mock host returns success but stores
  nothing → `WriteNotApplied`); settle/retry timing injected, not slept; truncation handled as
  failed read; byte-exact round-trip where declared; slice invalidated by chunk digest change is
  re-located, never reused.
- Modulation: slot identity; deterministic ambiguity + remaining list (reads); **ambiguous mutation
  selector refusal** (B-48); provider channel respected (parameter-only provider fixture writes
  parameters, state provider writes state); depth refusals; vocabulary policy + `force` echo;
  multi-unit sequencing enforced (N routes ⇒ N sequential verified units on the Vital fixture), with
  token/revision revalidation before each unit.
- Dependencies: each finding kind; hard ⇒ `write_calls_attempted == 0`; projected-world evaluation
  fixture (a plan that would be falsely refused against the unprojected snapshot must pass).
