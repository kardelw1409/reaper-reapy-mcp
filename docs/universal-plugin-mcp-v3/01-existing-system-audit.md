# 01 — Existing System Audit: `reaper_reapy_mcp` (target repository)

(rev. 2026-10-10, Phase C — see [18](18-remediation-register.md); the only Phase-C change to this
document is the accepted startup-lifecycle clarification in §3.)

Scope: the repository as it exists on 2026-10-10, branch `feature/vst-bridge`, HEAD `b638dc9`.
Every claim below is **[A] verified in source** unless tagged otherwise. Line numbers refer to the
files as read during Phase A.

## 1. Repository layout and packaging

```
C:\repos\reaper-reapy-mcp\
├── pyproject.toml              setuptools; name reaper_reapy_mcp; version 0.1.3; requires-python >=3.10
├── README.md                   user-facing feature list (57 tools documented in groups)
├── LICENSE                     MIT
├── reaper_side_enable_server.py  run inside REAPER: `import reapy; reapy.config.enable_dist_api()`
├── docs/media/                 demo gifs only (no written docs)
├── .github/workflows/python-publish.yml   PyPI publish on release (no test workflow)
├── src/
│   ├── __init__.py             re-exports ReaperController, setup_mcp_tools, main
│   ├── __main__.py             `from .reaper_reapy_mcp import main`
│   ├── reaper_reapy_mcp.py     legacy-compatible entry module (sys.path juggling + main())
│   ├── reaper_controller.py    back-compat wrapper: aggregates all controllers into ReaperController
│   ├── mcp_tools.py            1576 lines; setup_mcp_tools(mcp, controller); all 57 @mcp.tool closures
│   ├── controllers/            base, track, fx, marker, midi, audio, master, project
│   ├── reaper_reapy_mcp/       package entry: __init__.py, __main__.py, server.py
│   ├── utils/                  item_utils, position_utils, sample_audio
│   └── reaper_reapy_mcp.egg-info/   local build metadata
└── tests/                      7 test files (see §6)
```

Dependencies declared in `pyproject.toml` [A]: `python-reapy>=0.9.0`, `python-osc>=1.8.0`,
`pytest>=7.0.0`, `mcp[cli]>=0.1.0`, `python-dotenv>=1.0.0`, `requests>=2.31.0`.
There is **no test job in CI**; the only workflow builds and publishes a release on GitHub release
[.github/workflows/python-publish.yml] [A].

## 2. Bootstrap, entry points, transport

Two equivalent construction paths exist:

- `src/reaper_reapy_mcp/server.py::create_server(debug=True)` [A]:
  `controller = ReaperController(debug=debug)` → `mcp = FastMCP("Reaper Control")` →
  `setup_mcp_tools(mcp, controller)` → returned. `main()` calls `create_server(debug=True).run()`.
- `src/reaper_reapy_mcp.py::main()` [A]: same construction inline after `sys.path.insert` of the
  `src` directory; included for direct-script compatibility.

`src/reaper_reapy_mcp/__init__.py` re-exports `main`, `create_server`, `ReaperController`, and three
"core" helper functions used by REAPER-side scripts (`get_selected_items_core`,
`get_first_selected_audio_item_core`, `insert_midi_files_core`) [A].

**Transport [A]:** `FastMCP(...).run()` with no transport argument → the `mcp[cli]` FastMCP default
(stdio). The actual client registration confirms stdio:

`C:\REAPER\.vscode\mcp.json` [A]:
```json
"reaper": {
  "type": "stdio",
  "command": "py",
  "cwd": "C:\\repos\\reaper-reapy-mcp",
  "args": ["-3.11", "-c",
     "import sys; sys.path.insert(0, r'C:\\repos\\reaper-reapy-mcp\\src'); from reaper_reapy_mcp import main; main()"]
}
```
Consequences for V3: (a) the server is run **from source** (no install step); (b) Python is `py -3.11`;
(c) six other MCP servers are registered in the same client (reaper_project_analyzer, stem_splitter,
midi_writer, muscriptor, music_analysis) — the `reaper` server is one tool provider among several.
The user-level `mcp.json` (`C:\Users\karde\AppData\Roaming\Code\User\mcp.json`) is empty [A].
**No `vital` server is registered in this client config** [A] — the POC has its own launch story.

## 3. Host integration (reapy)

- `src/controllers/base_controller.py::BaseController.__init__` calls **`reapy.connect()` eagerly**
  and re-raises on failure [A:31-33]. The server therefore cannot start without a reachable REAPER.
  **Phase C (AUD-006 Option A):** this is the **accepted V1 lifecycle** — documented, unchanged; the
  plugin layer adds no eager connection and no promise that a tool can answer when the process never
  started ([13 §4/§6](13-existing-mcp-integration.md)).
- `BaseController.verify_connection()` returns `True` unconditionally (`# TODO correct checker`,
  `# return reapy.is_connected()` commented out) [A:36-43]. The `test_connection` tool inherits this.
- Track access goes through `reapy.Project().tracks[i]` with bounds validation
  (`_validate_track_index`) [A].
- One direct ReaScript call in base: `RPR.IsMediaItemSelected(item.id)` in `get_selected_items` [A].
- `reaper_side_enable_server.py` is a two-line REAPER-side script
  (`reapy.config.enable_dist_api()`) that must be run inside REAPER to enable reapy's server [A].

## 4. FX handling (the closest existing surface to V3)

`src/controllers/fx_controller.py` [A]:

| Concern | Implementation |
|---|---|
| Add FX | `track.add_fx(name)` (reapy high-level), retry with `' ['`-stripped base name; returns **`bool`** (`add_fx` :13-66) |
| Remove FX | `fx.delete()`; `track_index == -1` maps to `project.master_track` (:81) |
| Set param | `TrackFX_SetParamNormalized` (:158) then `TrackFX_GetParamNormalized` verify-read (:167); value treated as **normalized 0..1** |
| Param addressing | name match (whitespace-collapsed, lowercase, exact then substring), fallback to numeric index passed as string (:118-137) |
| Get param | `TrackFX_GetParamNormalized` (:269) |
| Param list | `fx.params[i].name/.value` via reapy; paged with `max_params`/`start_param`/`include_values` |
| FX list | `track.fxs` reapy list; master variant only via `MasterController.get_master_fx_list` |
| Available FX | parses `%APPDATA%\REAPER\reaper-vstplugins64.ini` etc. + `GetResourcePath()` — **name strings only**, no vendor/type/ident metadata (:472-505, `_clean_plugin_name` :608-639) |
| Sampler sample | `TrackFX_SetNamedConfigParm` with `"FILE"`/`"FILE0"` (:220) — the only named-config usage in the repo |
| FX automation item | `GetFXEnvelope`, `InsertAutomationItem`, `InsertEnvelopePointEx`, `Envelope_SortPointsEx` (:433-465) |

**Absent** (verified by repo-wide search) [A]: `GetTrackStateChunk`, `SetTrackStateChunk`,
`TrackFX_GetParamIdent`, `TrackFX_GetParamFromIdent`, `TrackFX_GetNumParams`, `TrackFX_GetParamName`,
`TrackFX_GetFXName`, `TrackFX_AddByName`, `TrackFX_Delete`. No plugin-state, ident, chunk or
capability logic exists anywhere in `src/` or `tests/`. The only "VST" logic is string cleaning
(`[VSTi]` suffix detection) in `_clean_plugin_name` [A:608-639].

**Verified defect [A]:** `src/mcp_tools.py:199-201` (`add_fx` wrapper) does
`fx_index = controller.add_fx(...)` — but `add_fx` returns `bool`. The check `if fx_index >= 0:` is
`True` for `False` (since `False >= 0`), so on failure the tool returns
`{"status": "success", "message": "... at index False"}`. Recorded here as evidence of the
"silence is not proof of success" class the V3 charter forbids; **not fixed by this plan** (out of
scope; the plugin layer does not reuse controller methods).

Master-track FX support is **partial** [A]: `track_index == -1` is honored in `remove_fx`,
`set_fx_param`, `load_sampler_sample`, `get_fx_param`, `get_fx_param_list` but **not** in `add_fx`,
`get_fx_list`, `toggle_fx`; a separate `MasterController` covers some master operations.

## 5. Tool surface (57 tools) and response semantics

Registration pattern [A]: `setup_mcp_tools(mcp, controller)` in `src/mcp_tools.py:7` defines *nested
closures* decorated with `@mcp.tool("name")`; every tool has the shape
`def name(ctx: Context, ...) -> Dict[str, Any]` and calls `controller.<method>(...)`.

Groups (counts verified; names verbatim from decorators):

- **Project/tempo/render (7):** `set_tempo`, `get_tempo`, `get_time_signature`,
  `set_project_time_signature`, `get_project_time_signature`, `set_time_signature`, `render_project`
- **Track (11):** `create_track`, `rename_track`, `move_track`, `set_track_volume`,
  `get_track_volume`, `create_track_send`, `set_track_color`, `get_track_color`, `set_track_folder`,
  `get_track_count`, `get_track_list`
- **FX, track level (10):** `add_fx`, `remove_fx`, `set_fx_param`, `load_sampler_sample`,
  `get_fx_param`, `get_fx_param_list`, `get_fx_list`, `get_available_fx_list`, `toggle_fx`,
  `add_fx_param_automation_item`
- **Master (7):** `get_master_track`, `set_master_volume`, `set_master_pan`, `add_master_fx`,
  `get_master_fx_list`, `toggle_master_mute`, `toggle_master_solo`
- **Markers/regions (5):** `create_region`, `delete_region`, `create_marker`, `delete_marker`,
  `get_region_list`
- **MIDI (7):** `create_midi_item`, `add_midi_note`, `add_midi_notes`, `clear_midi_item`,
  `get_midi_notes`, `find_midi_notes_by_pitch`, `get_selected_midi_item`
- **Audio/items (9):** `insert_audio_item`, `insert_midi_item`, `duplicate_item`,
  `get_item_properties`, `set_item_position`, `set_item_length`, `delete_item`,
  `get_items_in_time_range`, `get_selected_items`
- **Other (1):** `test_connection`

Three tools bypass the controller and touch reapy inline (`get_region_list`,
`get_track_list`, `get_items_in_time_range`) [A] — an existing inconsistency, not a pattern to copy.

Response semantics [A]:

1. Controllers catch broad exceptions, log, and return sentinels (`False`, `[]`, `None`, `0.0`).
2. Wrappers wrap calls in `try/except Exception` and return
   `{"status": "error", "message": str(e)}`; success is `{"status": "success", "message": ...}` plus
   ad-hoc extra keys.
3. There is no schema, no error code, no machine-readable error class, and no distinction between
   "wrong input", "host failure" and "write did not land" — all become a message string.

Item addressing uses two schemes ("sequential index" vs `direct_item_id` `MediaItem*0x...`)
documented in tool docstrings [A]. Position arguments accept seconds or `"measure:beat,fraction"`
via `utils/position_utils.py` (`position_to_time`, `time_to_measure`) [A].

## 6. Tests

| File | Nature | Offline? |
|---|---|---|
| `test_midi_mock.py` | `unittest` + `unittest.mock`; defines `MockReaperController`; MIDI/item paths | **Yes** (only fully offline test) [A] |
| `test_controller_structure.py` | script; builds `ReaperController`; exercises one method per controller | No — live REAPER [A] |
| `test_mcp_client.py` | despite the name, drives `ReaperController` directly (no MCP Client/stdio code found) | No [A] |
| `test_mcp_temporary.py` | script; live MIDI/item ops with sleeps | No [A] |
| `test_reaper_controller.py` | `unittest`; `setUpClass` **raises** if `verify_connection()` fails (which cannot fail, §3); ~12 methods | No [A] |
| `test_reapy_connection.py` | 8-line smoke: `reapy.connect()` + print | No [A] |
| `test_mcp.bat` | launches MCP inspector against the server via `uv` | N/A [A] |

There is no `conftest.py`, no pytest markers, no fixtures directory and no CI test run [A].
Consequence: the repository currently has **no offline regression suite** for tool contracts —
V3's testing strategy must create one ([12](12-testing-strategy.md)).

## 7. Working-tree state (evidence for "do not disturb")

`git status --porcelain -uall` on 2026-10-10 [A]:

```
M  src/controllers/project_controller.py     # STAGED modification: 26 insertions, 3 deletions
?? composer_v1_2_lm_probe.md                 # untracked 1165-byte probe report (VS Code LM probe)
```

- Branch `feature/vst-bridge` is at the same commit as `origin/main` (`b638dc9`); the branch carries
  **no additional commits** [A]. The branch name suggests intended VST work that has not started in
  commits.
- `composer_v1_2_lm_probe.md` is a probe report about VS Code's `vscode.lm` API and GPT-5.6 Luna
  (a failed `sendRequest` due to a missing session id) [A] — unrelated to plugin control, but it is
  untracked user work and must be left alone.
- **Implication for V3 phases:** the staged `project_controller.py` change is a pending user change
  to a file the plugin layer must not need; implementation phases must not amend, revert or "fix"
  it, and live tests must not depend on `project_controller` behavior beyond what is currently
  committed. See [15 R-11](15-risks-and-open-questions.md).

## 8. Integration points for V3 (summary; detail in [13](13-existing-mcp-integration.md))

1. **Server construction:** `create_server()` in `src/reaper_reapy_mcp/server.py` is the single place
   to add a second registration call (e.g. `setup_plugin_tools(mcp, ...)`) [A].
2. **Registration style:** `FastMCP.tool(name=...)` accepts plain callables; the POC proves the
   explicit-closure style works with `mcp[cli]` FastMCP [A: `vital/mcp/server.py:92`].
3. **Response envelope:** V3 tools will return a structured envelope with typed errors — a deliberate
   departure from the `{"status": ...}` convention, justified because plugin writes must carry
   per-item verification results ([06 §3](06-mcp-tool-contracts.md)).
4. **Transport:** stdio already; no new dependency is needed for transport; V3 adds no process.
5. **Names:** the 57 existing names are snake_case; new tools will be prefixed `plugin_`
   (`plugin_instances`, `plugin_describe`, `plugin_find`, `plugin_read`, `plugin_write`,
   `plugin_state`, `plugin_modulation`) — no collisions with the existing 57 [A].

## 9. Open questions about the existing system

1. `add_midi_notes` (mcp_tools.py:895) appears to contain no `controller.` call in a file-wide grep
   [D] — its internal path was not fully read; verify before relying on any statement about it.
2. `get_fx_param_list` paging semantics (`max_params`, `start_param`) [D] — partially read.
3. Whether `reapy`'s high-level `track.fxs` API is used anywhere in FX hot paths that V3 must not
   share (V3's host bridge will use raw `reascript_api` for parity with the POC) [D].
