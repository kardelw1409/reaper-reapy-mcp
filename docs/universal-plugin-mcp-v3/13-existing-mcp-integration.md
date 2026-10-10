# 13 — Existing MCP Integration

Status: **[D] proposed** (rev. 2026-10-10, Phase C remediation → Phase D consolidation → Phase E approvals and handoff — see [18](18-remediation-register.md), [19](19-owner-decisions.md), [20](20-implementation-handoff.md));
the integration surface itself is **[A] verified** (files/symbols below), with two Phase-C
resolutions: the **startup lifecycle** is AUD-006 Option A, and **registration is unconditional**
(AUD-013).

## 1. Confirmed integration point

| Fact | Evidence |
|---|---|
| One server process, constructed by `create_server(debug=True)` in `src/reaper_reapy_mcp/server.py` | [A: server.py] |
| Tools are registered by plain function calls on a shared `FastMCP("Reaper Control")` instance | [A: server.py; mcp_tools.py:7] |
| Transport is stdio (client config pins it) | [A: `C:\REAPER\.vscode\mcp.json`] |
| Launch is `py -3.11 -c "sys.path.insert(0, r'C:\repos\reaper-reapy-mcp\src'); from reaper_reapy_mcp import main; main()"`, cwd = repo root | [A] |
| The plugin layer can register additional tools with the same FastMCP instance, using the POC's proven SPEC-driven closure pattern | [A: `vital/mcp/server.py:57-95`] |

**Decision:** integration = one new call in `create_server()`:

```python
def create_server(debug: bool = True) -> FastMCP:
    ...
    controller = ReaperController(debug=debug)
    mcp = FastMCP("Reaper Control")
    setup_mcp_tools(mcp, controller)
    setup_plugin_tools(mcp)          # NEW — no-op when no provider is configured
    return mcp
```

`setup_plugin_tools(mcp, config=None)` lives in `src/reaper_reapy_mcp/plugins/tools.py` and registers
the 7 `plugin_*` tools **unconditionally** (AUD-013): with no applicable provider configured, every
plugin tool returns a typed `UnsupportedProvider` result — registration never depends on provider
discovery or configuration state. It must **not** construct a host connection, must **not** import
`reapy` at module import time, and must **not** raise when provider configuration is absent.

## 2. What the plugin layer may and may not take from the repo

| May reuse | Must not reuse |
|---|---|
| `FastMCP` instance and registration API (already proven compatible with `mcp[cli]`) | `ReaperController`/controller methods (bool sentinels, swallowed exceptions — [01 §4] [A]) |
| The repo's packaging basics (src layout; `pyproject.toml` for deps) | Existing error convention `{"status": "error", "message": str}` for plugin tools |
| `utils/position_utils` if ever needed (not needed in V1) | Existing test structure (no offline suite exists today — V3 creates one [01 §6]) |
| `reaper_side_enable_server.py` prerequisite (reapy dist API inside REAPER) | Any assumption that the FX layer's name-substring addressing is adequate for parameters |

**Why the controller path is refused (evidence, not taste):**

1. Controllers convert failures into `False`/`[]`/`None` then into human strings — the plugin design
   requires typed failures and counters (`write_calls_attempted: 0` on refusals), which cannot be
   recovered after the fact [A: 01 §4].
2. The repo contains a live defect of exactly this class: `add_fx` reports success with index `False`
   because a bool is compared with `>= 0` [A: mcp_tools.py:199-201].
3. Controllers expose no batch parameter primitive and no state/ident surface [A: 01 §4] — the very
   gap the POC documented as its reason for not integrating (DEC-01) [A].

## 3. Registration mechanics (adapted from the proven POC pattern)

- A module-level table: `TOOL_NAMES`, `SPEC` (purpose, args, required, destructive), `DISPATCH`.
- `setup_plugin_tools(mcp)` loops the table, wraps each dispatch function in a closure that:
  1. validates required args → envelope refusal (never a traceback);
  2. catches `TypeError` → envelope refusal with accepted args;
  3. calls the tool body → envelope.
- `server.tool(name=name)(tool)` with `tool.__name__ = name` and a synthesized docstring
  (purpose/args/destructive/return contract) [A: POC mcp/server.py:57-95].
- **Name collision guard:** a test asserts the 7 plugin names ∩ the 57 existing names = ∅
  (and that FastMCP exposes 64 tools after registration).

## 4. Runtime dependencies and initialization order

| Concern | Rule |
|---|---|
| Import time | `plugins/__init__.py` must import no `reapy` and must not read provider config eagerly; configuration is read on first plugin-tool call |
| Host connection | the plugin host bridge connects lazily (no second eager connection, no connection at import); **the process itself still requires REAPER at startup** because `BaseController.__init__` calls `reapy.connect()` eagerly (pre-existing behaviour, unchanged — AUD-006 **Option A**). Post-startup connection loss ⇒ the affected call returns a typed `NotConnected` |
| Dialog handling | optional; default **conservative** (capture dialog text, no auto-dismiss of questions); live-write tests enable the aggressive in-flight policy explicitly [A: POC dialogs; policy adapted per [08 §4, item 6](08-provider-and-host-interfaces.md)] |
| Per-instance lock | created per resolved instance inside the plugin layer; never global |
| Provider loading | in-repo provider package `plugins/providers/vital/` (Phase-D recommended — [19 Decision 2](19-owner-decisions.md)); enabled by id via `REAPER_MCP_PLUGIN_PROVIDERS`; unset ⇒ zero providers. Registry data path: `REAPER_MCP_VITAL_REGISTRY_DIR` (default the POC registry directory), digest recorded in the profile provenance |
| Cache root | `REAPER_MCP_PLUGIN_CACHE` env var with a documented default under the user profile ([09 §4]) |

## 5. Error propagation (the compatibility question)

The existing 57 tools return `{"status": "success"|"error", ...}`; V3 tools return the envelope
([06 §2]). Two conventions coexist **because they are different tools**:

- Existing tools: unchanged, byte-for-byte behaviour, no re-wrapping.
- Plugin tools: envelope with `ok`, typed `error`, `warnings`, `meta` (counters, instance, profile key).

Rationale: plugin operations return structured verification data (per-item rows, rollback outcome)
that the status-string convention cannot carry; and "silence is not proof of success" is the layer's
central invariant [A: POC DEC-22]. Registering the plugin tools with the status shape would force
lossy projection of exactly the information the layer exists to provide.

Compatibility mitigations:
1. The envelope is documented in the plugin tools' synthesized docstrings (the LLM sees it).
2. `plugin_*` names are disjoint; nothing existing changes.
3. A future harmonization of the 57 tools' error shape is explicitly **out of scope** ([00 §3]).

## 6. Compatibility matrix

| Dimension | Status | Note |
|---|---|---|
| Tool names | disjoint (`plugin_*`) | guarded by test |
| Transport | stdio, unchanged | no new process/port |
| Python | `py -3.11` launch | existing repo declares `>=3.10`; plugin layer must stay 3.10-compatible in syntax unless the owner raises the floor |
| Dependencies | no new mandatory deps | plugin layer uses stdlib + existing `reapy`/`mcp`; tests use `pytest` (already a dependency) |
| Startup time | +import cost only | tools register lazily; no host call at startup |
| REAPER closed at startup | **process does not start** (pre-existing eager `reapy.connect()` raises) — the accepted V1 lifecycle ([19 Decision 4](19-owner-decisions.md), AUD-006 Option A) | the plugin layer adds no further eager connection, and a connection lost after startup yields a typed `NotConnected` [A: 01 §3]; changing this (lazy startup, Option B) is out of V1 scope and would require re-planning |
| Working tree | **do not touch** staged `project_controller.py` change or the untracked probe doc | [01 §7] |

## 7. Rollout plan (implementation session, P0–P10; phase IDs per [14](14-implementation-plan.md), AUD-011)

1. **P0-P2 (offline):** land the `plugins/` package, domain models + target-token/revision objects,
   mock provider/host/codec-B, and the mock-proven transaction — **no server change yet**; offline
   suites green; **tool registration/schema tests run against mocks from P2 onward** (early
   registration tests, AUD-011).
2. **P3-P4 (live read-only):** target-aware bridge + profile/cache; L3 read validation; no writes
   exist.
3. **P5-P6 (offline write contract):** write primitives + codec contract (captured-chunk splice
   fixtures only — **no live mutation**); mock modulation/dependency validation.
4. **P7 (provider):** the in-repo, dependency-isolated Vital provider (codec extraction per
   [19 Decision 2](19-owner-decisions.md), AUD-005); V-1…V-12 regressions; clean-process
   import boundary; L3 read through the provider.
5. **P8 (first live writes):** the full-channel self-restoring L4 harness on the designated test
   project; evidence recorded; every channel compared before "restored" is claimed.
6. **P9 (registration):** add `setup_plugin_tools` to `create_server()`; verify the server starts and
   exposes 64 tools via inspector (`test_mcp.bat` exists [A]); existing 57 tools unchanged (smoke via
   inspector); `UnsupportedProvider` behaviour with zero providers.
7. **P10 (acceptance + docs):** the acceptance scenario per [14 P10](14-implementation-plan.md);
   README section describing the plugin tools + provider configuration; owner sign-off recorded.

Rollback of the integration itself is trivial by construction: removing the one `setup_plugin_tools`
call restores the previous server exactly (no existing code paths were modified).

## 8. Integration risks (detail in [15](15-risks-and-open-questions.md))

| # | Risk | Mitigation |
|---|---|---|
| R-1 | Provider import failure at call time (path typo, POC tree moved) | typed `UnsupportedProvider` refusal with config echo; startup unaffected |
| R-2 | Plugin tool latency exceeds client patience (Vital multi-route writes ~2.3 s/route [C]) | batch-first + `read_cost_next: 0`; document expected durations in tool docs; consider progress meta in V2 |
| R-3 | Two error conventions confuse the LLM | docstrings declare the envelope (schema 2, `error` + `outcome`) explicitly; tool names prefix-signal the family |
| R-4 | Dialog watcher policy too aggressive in shared use | conservative default; aggressive only via explicit config for test runs |
| R-5 | Eager `reapy.connect()` at server start (pre-existing) means the process requires REAPER; plugin tools cannot answer at all if the process never started | accepted V1 lifecycle (AUD-006 **Option A**): documented, no impossible promise; the plugin layer adds no eager connection and is internally lazy for testability; Option B (lazy startup) is an explicit owner decision if wanted |
