# 17 — Planning Evidence Index

(rev. 2026-10-11, Phase C remediation — see [18](18-remediation-register.md); §J added with the
direct source verification of the audit findings, plus AV-13 with the Phase-D codec purity map.)

Every significant claim/decision in this documentation set, with its evidence. Columns:

- **Cat** — [A] verified in source · [B] verified by tests · [C] observed runtime/forensic · [D] proposed/inferred/unresolved.
- **Verified by** — `P` primary author read the symbol directly; `S+P` subagent located it and the primary
  author cross-checked the decisive part; `S` subagent report only (not independently re-read).
- **Conf** — confidence: high / med / low.
- Line numbers are from Phase A reads (2026-10-10) and may drift.

## A. Target repository (`C:\repos\reaper-reapy-mcp`)

| ID | Claim / decision | Evidence | Cat | Verified by | Conf | Used in |
|---|---|---|---|---|---|---|
| T-01 | `create_server()` builds controller → `FastMCP("Reaper Control")` → `setup_mcp_tools` | `src/reaper_reapy_mcp/server.py` | A | P | high | 01 §2, 13 §1 |
| T-02 | Launch = stdio via `py -3.11 -c … from reaper_reapy_mcp import main` from repo cwd | `C:\REAPER\.vscode\mcp.json` | A | P | high | 01 §2, 13 §1 |
| T-03 | 57 tools registered as closures with `@mcp.tool("name")` inside `setup_mcp_tools` | `src/mcp_tools.py:7`; 57 decorator hits | A | P (count) / S+P (grouping) | high | 01 §5, 13 §3 |
| T-04 | Tool errors: catch-all → `{"status":"error","message":str}`; controllers return bool/[]/None | `src/mcp_tools.py` pattern; `controllers/fx_controller.py:63-66` | A | S+P | high | 01 §5, 13 §2 |
| T-05 | `BaseController.__init__` calls `reapy.connect()` eagerly; failure raises | `src/controllers/base_controller.py:31-33` | A | P | high | 01 §3, 13 §4 |
| T-06 | `verify_connection()` always returns True (TODO) | `base_controller.py:36-43` | A | P | high | 01 §3, 15 R-04/Q-04 |
| T-07 | `add_fx` returns bool; wrapper `if fx_index >= 0` → fake success `index False` | `fx_controller.py:13`; `mcp_tools.py:199-201` | A | P | high | 01 §4, 13 §2 |
| T-08 | Param addressing: name substring then index-as-string; value normalized; no ident support | `fx_controller.py:104-170` | A | S+P | high | 01 §4 |
| T-09 | No chunk/ident/plugin-state logic anywhere in repo | repo-wide grep (GetTrackStateChunk/GetParamIdent/Vital) | A | S+P | high | 01 §4, 13 §2 |
| T-10 | `get_available_fx_list` parses REAPER ini files; names only | `fx_controller.py:472-505` | A | S+P | high | 01 §4 |
| T-11 | Master FX support partial (`-1` honoured in 5 methods, not in add_fx/get_fx_list/toggle_fx) | `fx_controller.py` | A | S | med | 01 §4 |
| T-12 | Tests: only `test_midi_mock.py` offline; others live; no conftest/CI test job | `tests/`; `.github/workflows/python-publish.yml` | A | S+P | high | 01 §6, 12 §6 |
| T-13 | Staged change `M  src/controllers/project_controller.py` (26+/3-), untracked `composer_v1_2_lm_probe.md`; branch `feature/vst-bridge` == main tip | `git status --porcelain -uall`, `git diff --cached --numstat`, `git log` | A | P | high | 01 §7, 14 preamble, 15 R-10 |
| T-14 | Plugin tools integrate via one `setup_plugin_tools(mcp)` call | proposal on `server.py` facts | D | P | high | 13 §1 |
| T-15 | Envelope divergence for plugin tools is justified (structured verification data) | proposal on T-04 evidence | D | P | med | 13 §5 |

## B. POC transaction / safety core

| ID | Claim / decision | Evidence | Cat | Verified by | Conf | Used in |
|---|---|---|---|---|---|---|
| E-01 | Order: snapshot → validate → document write → parameter batch → verify → commit; rollback on failure | `adapter/transaction.py` (`apply` :165; `_write_document` :358; `_verify` :449; `_rollback` :503) | A | S+P (flow cross-checked via `llm.submit` P) | high | 02 §12, 11 §2 |
| E-02 | Order not configurable; reversed order refused (`WriteOrderViolation`) | `transaction.py:172, 607-622` | A | S | high | 11 §2.4 |
| E-03 | Snapshot = deepcopy(doc) + framing + routes + planned parameter indices; memory only | `transaction.py:180-192` | A | S | high | 11 §2.3 |
| E-04 | Success = verified read-back; `SetTrackStateChunk` return ignored | `transaction.py` docstrings/`_verify` `why` field; `llm.submit` note "committed after a verified read-back on both channels" | A | S+P | high | 02 §12, 11 §2.5 |
| E-05 | Tolerances: state 1e-5, parameter 1e-3; executor restore 1e-6 both channels | `transaction.py:469,490`; `executor.py:1101` | A | S | high | 11 §2.5/2.7, 12 §8 V-6 |
| E-06 | Rollback verified by predicate (routes/document equality); unverified → `RollbackFailed` | `transaction.py:520-527` | A | S | high | 11 §2.7 |
| E-07 | Parameters not restored; `parameters_restored:False` + `parameter_gap` (DEC-19) | `transaction.py:539-541` | A | S | high | 11 §2.3, 15 R-19 |
| E-08 | Document write resets ordinary parameters (cutoff 0.20→0.54688) — the order's reason | POC transaction docstring; `vital/README.md` §"The write order" | A+C | S+P (README read P) | high | 11 §2.4, 04 §3.2 |
| E-09 | One route per state write; sequential writes land 8/8; measured for 2-8 routes | `vital/README.md` §"One route per state write"; `transaction._write_document` | A+C | P (README) | high | 10 §1.2 (rule 6), 11 §2.4 |
| E-10 | Verify compares in the **written space**; the first version compared normalized vs display and reported 3 false mismatches | `llm._verify_batch` docstring | A | P | high | 11 §2.5, 12 §10 |
| E-11 | `submit()` refusals: `refused_before_write`, `host_reads:0`, `host_writes:0`; dry-run skips writes | `llm.py:769-905` | A | P | high | 06 §7, 11 §2.1 |
| E-12 | `plugin_write_batch` = preflight → submit → `_verify_batch`; `read_cost_next.needed = 0`; dry_run default False | `llm.py:1106-1158` | A | P | high | 02 §4, 06 §7 |
| E-13 | Op-stream executor verdicts incl. `REFUSED_BEFORE_ANY_WRITE`, `FAILED_AND_(NOT_)RESTORED`; authorise gate writes nothing on refusal | `adapter/executor.py` (`authorise` :758, `_verdict` :1117) | A | S | high | 04 §5, 11 §3 |
| E-14 | Plan is data; nothing written by `plan.build`; projection against live state prevents false refusal | `plan.py:318,505,535`; `dependencies.check_plan` | A | S | high | 04 §3.2, 11 §2.2 |
| E-15 | `SetTrackStateChunk` return recorded but never trusted; decode-verify with settle 0.4 s / retry 2-3 s | `bridge/state.py:81,224-320`; `transaction.py` call sites | A | S (two independent reports agree) | high | 10 §1.2 |
| E-16 | A restore point must not be rebuilt from an already-patched instance (snapshot-of-snapshot destroys the way back) | repo evidence lesson recorded in project memory; POC DEC-19 manifest discipline | C | P (memory) + S | med | 11 §2.3 |

## C. POC capability layer / cache / values

| ID | Claim / decision | Evidence | Cat | Verified by | Conf | Used in |
|---|---|---|---|---|---|---|
| C-01 | Cache key = identity fields + registry digests + format key; required `vst_class`+`param_count` | `capability_cache.py:75-97,148` | A | P | high | 09 §3, 07 §3 |
| C-02 | Cache key = identity + registry digests + format; five refusal cases; truncation fails two checks | `capability_cache.py:75-97,148,300-424` | A | P | high | 09 §3/§5 — **corrected in Phase C:** `save()` overwrites same-path files without identity validation and writes non-atomically; foreign-preservation + atomicity are V3 requirements, not inherited (AUD-012) |
| C-03 | Cache storage under `logs/capability_cache/`; deletable derived data | `capability_cache.py:57` | A | P | high | 09 §4 |
| C-04 | Forensic accounting: 192 calls, 102 capability, 98 single-control, ~110 static-answerable | `capability_cache.py` docstring (recorded 2026-09-28 run) | C | P | high (as recorded) | 09 §1, 04 I-6 |
| C-05 | Control record fields incl. `ident`; `kind` incl. `none/unmeasured` first-class | `adapter/controls.py:46,67`; `spaces/laws.py` | A | S | high | 02 §6 |
| C-06 | Law kinds affine/enum_index/exponential/identity/none; stated ranges; fit priority; evidence strength | `spaces/laws.py` | A | S | high | 02 §6, 07 §2 |
| C-07 | Display derived offline from `vital_scales_full.json` (not the 9-10× host formatter) | `registry.py` docstring; `parameters.py` | A | S | med | 02 §6 |
| C-08 | No alias table; name resolution accepts registry name/state key + `suggest()` | `controls.py` (absence), `plan.resolve_control_name`, `registry.ControlTable.suggest` | A/D | S | med | 05 §1, 07 §6 |
| C-09 | Registry files: correspondence 778 rows, controls 778, scales, vocabulary (2226-preset corpus); no runtime staleness check | `registry.py`, `_env.py:54-64` | A | S | high | 02 §15, 09 §3 |
| C-10 | `REAPY_MCP_SRC` = `C:\repos\reaper-reapy-mcp\src`, used for legacy imports w/ cwd preservation | `_env.py:59-60,97,109-125` | A | P | high | 02 §15, 15 R-03 |

## D. POC identity / host transport / dialogs

| ID | Claim / decision | Evidence | Cat | Verified by | Conf | Used in |
|---|---|---|---|---|---|---|
| H-01 | Identity = track+fx + fx_name + class hex + module + param_count + version; GUID never identity | `bridge/locate.py:36-73,166-215` | A | S | high | 02 §5, 09 §2 |
| H-02 | Ambiguity raises; selector supported; zero → InstanceNotFound (VST2 special message) | `locate.py:260-306,352` | A | S | high | 02 §5 |
| H-03 | Version gate `MEASURED_VERSIONS=("1.5.5",)`; advisory at resolve, hard where asserted | `locate.py:73,377,396,467` | A | S | high | 07 §3, 15 Q-03 |
| H-04 | Host calls funnel through `bindings.call` (timeout/wedge/dialog capture) | `bindings.py:154-260` | A | S | high | 08 §4.2 |
| H-05 | Batch param IO inside REAPER (`@reapy.inside_reaper`), ident resolved in-hop, read-back incl. ident, no index cache | `bridge/parameters.py:82-231,275,297-366` | A | S | high | 08 §4.3-4.4, 02 §13 |
| H-06 | Host APIs used: TrackFX_Get/SetParamNormalized, GetParamIdent, GetParamFromIdent, GetNumParams, GetTrackStateChunk/SetTrackStateChunk; envelope helpers | `parameters.py`, `state.py`, `bindings.py` | A | S (agrees with brief §5.1 claims) | high | 02 §13, brief §5.1 |
| H-07 | Dialog watchdog: Win32 `#32770`, aggressive in-flight / conservative idle, capture-before-dismiss, `suppress_reapy_dialogs` | `bridge/dialogs.py` | A | S | high | 08 §4.6, 13 §4 |
| H-08 | `state.py` is mixed: chunk transport + Vital base64-JSON codec via legacy `_b4_lib` | `bridge/state.py`, `_env.import_legacy` | A | S+P | high | 10 §1, 03 matrix |
| H-09 | fxchain repairs FX build; VST2/VST3 discriminated by module string (same class id) | `bridge/fxchain.py`, `locate.py:62-63` | A | S | high | 02 §13 |
| H-10 | Measured cost: in-process batch 0.14 ms vs 94.6 ms socket (~680×); route writes ~2.3 s/route; lock needed at ~0.35 s spacing | POC FINAL_ARCHITECTURE §4.3-4.4 (as reported by digest) | C | S | med | 04 I-13, 15 R-05 |

## E. POC modulation / dependencies / state ops

| ID | Claim / decision | Evidence | Cat | Verified by | Conf | Used in |
|---|---|---|---|---|---|---|
| M-01 | Route identity = slot 1..64; no id field; deterministic first-match + remaining list | `adapter/model.py:349`; `modulations.Selector/select` | A | S | high | 10 §2.1 |
| M-02 | All 7 route fields written via document; parameter mirrors are read-only | `modulations.ROUTE_FIELDS`, `apply_changes_to_document` | A | S | high | 10 §2.1 |
| M-03 | Depth conversion per destination from measured display fit; 246/316 accepted; refusals named | `capabilities.depth_fit/modulation_depth` | A | S | high | 10 §2.2, 12 V-4 |
| M-04 | Vocabulary: 31 sources / 316 destinations; source-family precedent policy; `force` override | `registry.vocab().validate`, `modulation_vocabulary.json` | A | S | high | 10 §2.3 |
| M-05 | `line_mapping` write unsupported on 1.5.5 (stripped); requested curves refused; edit warns | `LINE_MAPPING_WRITE_SUPPORTED=False`; DEC-25; README §Two measured constraints | A+C | S+P | high | 10 §2.4 |
| M-06 | Dependency findings kinds hard/soft/incompatible/warning; `raise_on` pre-write; DISABLE set for structural transitions | `dependencies.check_plan/raise_on/DISABLE_CANDIDATES` | A | S | high | 10 §3 |
| M-07 | Wavetable selection is state-level (no param among 775); growth refused by measured policy | `wavetables.py`; README; in-code constants | A+C | S | high | 10 §1.3 |
| M-08 | Rooted-path requirement (`DOCUMENT_TOP_KEYS`); unrooted writes silently land nowhere | `modulations.parse_path/_set_path`; `plan` scope checks | A | S | high | 10 §1.2 |
| M-09 | `plan.build` returns `findings: []` unfilled (vestigial?) | `plan.py:318` region | D | S | low | 02 §19 |

## F. POC tool surface / envelope / tests / docs

| ID | Claim / decision | Evidence | Cat | Verified by | Conf | Used in |
|---|---|---|---|---|---|---|
| S-01 | 24 tools in `TOOL_NAMES`; README's "12 tools" is stale | `mcp/tools.py:45-61` | A | P | high | 02 §3, README disclosure |
| S-02 | Explicit SPEC-driven registration with required-arg validation and synthesized docs | `mcp/server.py:57-95` | A | P | high | 06 §10, 13 §3 |
| S-03 | Envelope ok/fail/dry with schema v1, sorted-key dumps | `mcp/results.py` | A | S+P | high | 06 §2 |
| S-04 | CLI reuses tool functions via `T.DISPATCH`; CLI-only escape paths exist | `cli/main.py` | A | S | high | 02 §2 |
| S-05 | 32 test scripts + ~65 probes; no pytest/conftest/runner; layer convention L1/L3/L4; `check()` per file | `tests/` inventory; README run section | A | S | high | 02 §16, 12 §2 |
| S-06 | L4 restores the **document and routes** after every case, with the route list predicate-verified; refuses on unresponsive plugin (exit 2) | `test_l4_live_write.py:82-141` | A | S+P | high | 12 §7 — **corrected in Phase C:** the captured control vector is never applied/compared (AUD-015); "self-restoring" means doc+routes only |
| S-07 | Artifacts under `prompts/vital_sound_design/logs/`; per-run naming `<kind>__<UTC>Z__<hash>`; two-writer rule | `probe_common.py:145-160`; `_env.LOGS` | A | S | med | 12 §5 |
| S-08 | Contract/consistency gates over recorded evidence (integrity baseline reproduced exactly; pinned live arithmetic) | `test_integrity.py`, `test_activation_4g4.py` etc. | A | S | high | 12 §1-§2 |
| S-09 | Running probes without a live host can overwrite recorded PASS evidence | project memory + `probe_cli.py`-family write behavior | C | P (memory) + S | high | 12 §1 |
| S-10 | POC deliberately separate from the REAPER MCP, two recorded reasons (swallowed exceptions; no batch/preset surface) — DEC-01 | `vital/mcp/server.py` docstring; DECISIONS.md | A | P | high | 00 §1, 13 §2 |

## G. POC canonical documents (claims *about* the documents)

| ID | Claim / decision | Evidence | Cat | Verified by | Conf | Used in |
|---|---|---|---|---|---|---|
| D-01 | `FINAL_ARCHITECTURE.md` is authoritative and supersedes the Phase-1 docs (its §16 lists 8 differences) | digest of `architecture/` | A | S | high | 02 §17 |
| D-02 | DEC-22 (success = verified read-back; no swallowing) is the flagship universal decision | `DECISIONS.md` | A | S | high | 04 I-1, 11 §2.5 |
| D-03 | DEC-19 named-gap snapshot policy; DEC-11 never-pointer identity; DEC-08 slot identity | `DECISIONS.md` | A | S | high | 11 §2.3, 09 §2, 10 §2.1 |
| D-04 | Recorded corrections: DEC-23 (stereo audibility claim wrong) and DEC-25 (corpus counts stale) | `DECISIONS.md` M10 updates | C | S | high | 12 §1 (evidence discipline) |
| D-05 | Canonical docs classify their own open questions (e.g. DEC-24 VST3 ident across version change OPEN) | `DECISIONS.md` | A | S | high | 15 Q-03 |
| D-06 | `sound_design_kb` is plugin-agnostic sound-design knowledge, never loaded at runtime | KB README + PHASE_4_KNOWLEDGE_ARCHITECTURE | A | S | high | 03 (out of scope), 02 §1 |

## H. Claims carried from the project brief (verified where possible)

| ID | Brief claim | Verification outcome | Cat | Used in |
|---|---|---|---|---|
| B-01 | Chain: LLM → (vit CLI) → `vital.mcp.tools` → Adapter → bridge → REAPER; CLI used the same functions | **Confirmed** in source | A | 02 §2 |
| B-02 | Host calls: TrackFX_Get/SetParamNormalized, GetParamIdent, GetParamFromIdent, GetTrackStateChunk, SetTrackStateChunk | **All confirmed** | A | 02 §13 |
| B-03 | "37 Adapter-facing calls, 15 host access" (from terminal history) | **Not verified**; superseded by the POC's own recorded accounting (192 calls, 102 capability) — the 37 figure should not be cited as a benchmark | D | 09 §1 |
| B-04 | `bridge/state.py` is a mixed component that understands the Vital document format | **Confirmed** | A | 03, 10 §1 |
| B-05 | Wavetable selection is state-level; Wave Frame is a parameter | **Confirmed** | A | 10 §1.3 |
| B-06 | Destination-specific depth conversion factors exist | **Confirmed** (fitted per destination; refusals where non-affine) | A | 10 §2.2 |
| B-07 | Instance identity demonstrated as REAPER track + FX slot; raw GUID not recommended | **Confirmed** (GUID read, never identity) | A | 09 §2 |
| B-08 | `plugin_write_batch` performed preflight → live reads → plan → transaction → write → verify in one call | **Confirmed** | A | 06 §7 |
| B-09 | POC test coverage exists but "do not claim coverage merely because a test file exists" | No POC suite was executed in Phase A; coverage claims avoided; [B] tags used only for the existence/meaning of specific tests, never for pass status | — | README evidence discipline |
| B-10 | A GLM 5.3 Flash subagent was named as optional | Used for 8 bounded read-only research tasks; disclosed in README | — | README §Research assistance |

## I. Phase-A process facts (self-audit)

| ID | Fact | Evidence | Cat |
|---|---|---|---|
| X-01 | No runtime source was modified; the only files created are under `docs/universal-plugin-mcp-v3/` | `git status` in target repo unchanged (staged user change + untracked probe only) | A |
| X-02 | No tests were executed (POC or target) | process | A |
| X-03 | No REAPER project or plugin state was touched | process | A |
| X-04 | Subagent research was read-only; no subagent edited any file | instructions in each delegated task; outputs were reports | A |
| X-05 | `git diff` (unstaged) empty + `git diff --cached` shows 26/3 for `project_controller.py` — the change is the user's staged work | direct commands | A |

## J. Phase C audit verification (2026-10-10) — direct source reads

These rows record the **independent verification of the audit's source claims** performed before any
documentation correction (the audit's diagnosis is not taken on trust). Everything below was read
directly by the primary author; none of it was executed (no tests, no REAPER).

| ID | Verified fact | Evidence (path:lines) | Cat | Used in |
|---|---|---|---|---|
| AV-01 | `find_vst_block` selects the **first** `<VST ` block of a chunk; `parse_framing` extracts item A/B from that block | `_b4_lib.py:108-111,113-158` | A | AUD-001; 02 §10; 10 §1 |
| AV-02 | `bridge/state.py::read_document` passes the **full chunk** to `b4.parse_framing(chunk)` while its comment claims a single-block view; the `select_block` result is used only for `block_header`/`block_class_hex`/`block_is_first_vst` reporting fields | `bridge/state.py` (`read_document`, `select_block` call + return dict) | A | AUD-001; 02 §10 |
| AV-03 | `write_document` rebuilds via `b4.rebuild_chunk(before["framing"], doc)` (first-block head/tail splice) and writes the whole chunk back via `SetTrackStateChunk` | `bridge/state.py` (`write_document` loop); `_b4_lib.rebuild_chunk` | A | AUD-001 |
| AV-04 | `select_block` raises `Unreadable` when no Vital block exists or the ordinal exceeds the Vital-block count — protection for identification, **not** for the byte path | `bridge/state.py::select_block` | A | AUD-001 |
| AV-05 | `_b4_lib` imports `_bh_session` and `_b3_lib` at module level; `_bh_session` at module level imports `reapy`, `ReaperController`, `vital_analysis/dialog_guard/render`, calls `os.chdir(r"C:\repos\reaper-reapy-mcp\src")`, and reads registry JSON | `_b4_lib.py:72-73`; `_bh_session.py:39-40,49-57,66-67` | A | AUD-005; 02 §15; 03 matrix |
| AV-06 | `capability_cache.save()` reads any existing file only for reporting and then overwrites it with a direct `open(path,"w")`; no identity check, no quarantine, no temp-file/rename | `capability_cache.py:408-417` | A | AUD-012; 02 §7; 09 §4-§5 |
| AV-07 | `build_or_load` on an unusable payload rebuilds and calls `save()` at the same path — the refused file is replaced | `capability_cache.py:427-476` | A | AUD-012 |
| AV-08 | L4 `snapshot()` stores a full control vector; `restore()` applies document+routes only and verifies the route list; controls are never re-applied or compared | `test_l4_live_write.py:101-141` | A | AUD-015; 02 §16; 12 §7 |
| AV-09 | No write-target token or state-revision concept exists in the POC (checked `locate.py`, `state.py`, `transaction.py`) — protection is fingerprint compare-on-use only | absence across those modules | A | AUD-003; 02 §5 |
| AV-10 | `TestTrackStateChunk`-level write verification in the POC decodes the (possibly first-block) document — the wrong document is also the verification subject when targeting is wrong | `bridge/state.py::_verify`/`_try_decode` + AV-02 | A | AUD-001; 10 §1; 11 §2.5 |
| AV-11 | The POC's route writes reach `modulations.plan_create/update/remove` through the **document channel** and the transaction; the "first match in slot order" behaviour lives in `Selector/select` and is used by read *and* plan paths | `modulations.py` (`Selector`, `select`, `plan_*`) | A | AUD-009; 10 §2.1 |
| AV-12 | The POC's parameter batch is one in-process hop (`@reapy.inside_reaper`), so a full-vector capture is *feasible*; its measured cost is **not recorded** in Phase-A evidence | `bridge/parameters.py`; no cost artifact cited | A/D | AUD-002; 02 §19.9; 11 §2.3 |
| AV-13 | **Codec purity map (Phase D):** in `_b4_lib.py` only `_get_chunk` (75-78 aliases, 97-98), `_set_chunk` (103-104) and `_default_ctrl` (502-505) touch host globals; the byte codec and routing helpers import nothing and reference no module scope beyond their arguments: `_b64d` (92), `find_vst_block` (108), `parse_framing` (115), `decode_document` (165), `encode_item_b` (175), `encode_item_a` (231), `wrap_lines` (256), `rebuild_chunk` (260), `_dig` (377), `list_routings` (396), `find_free_slot` (421), `make_routing` (431), `clear_routing` (463), `export_document` (509) | `_b4_lib.py` (line numbers from a Phase-D read) | P | [19 Decision 2](19-owner-decisions.md); 08 §6; 14 P7 |

Status note: these are **documentation corrections grounded in source verification**. They do not
constitute implementation or runtime validation; the register in [18](18-remediation-register.md)
tracks each finding to `CORRECTED_IN_DOCS`, not `VERIFIED`.
