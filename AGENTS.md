# AGENTS.md — working rules for this repository

For coding agents (and humans) changing `reaper-reapy-mcp`. These are the rules that are easy to
violate by accident; the rationale and measurements live in
[`docs/universal-plugin-mcp-v3/12-testing-strategy.md`](docs/universal-plugin-mcp-v3/12-testing-strategy.md) §11
(evidence policy) and `IMPLEMENTATION-STATUS.md` (what each round measured).

## Evidence and run artifacts (P32 — do not regress this)

1. **Never add a timestamped run artifact to Git.** Raw live-run results — evidence JSON, render
   WAV/PNG, profiles, caches, recovery points — are *generated evidence*: they belong in the
   run-artifacts directory, not in a commit. The historical corpus that predated this policy
   (`tests/plugins/live/evidence/`, 200 JSON + 12 PNG + 147 WAV) was **removed from the repository
   and from history in P33** and lives in the external research archive; record names remain valid
   as ids, and the ids are what the journal, the register and some source docstrings cite.
2. **A new live measurement writes outside the repository by default.** Use
   `tests/plugins/live_harness.py`: `write_run_json(...)` / `finalize_run(...)` write to
   `%LOCALAPPDATA%\reaper_reapy_mcp\run_artifacts\<stamp>__<task>__<pid>\evidence\`.
   Overrides: `REAPER_MCP_EVIDENCE_DIR` (exactly that directory), `REAPER_MCP_RUN_ARTIFACTS`
   (a root that still gets one directory per run), `REAPER_MCP_RUN_TASK` (task id).
   Do not build a second storage scheme — extend this one.
3. **Every diagnostic run leaves a short Markdown report.** Call
   `finalize_run(kind, payload, purpose=…, findings=…, limitations=…, tests=…, directory=…)`;
   it derives status/start/cost from the payload and writes `report.md` + `run.json` beside the
   evidence. A run that only calls `write_run_json` still gets an automatic summary at exit, but
   the explicit report (findings, limitations, test counts) is the useful one.
4. **Do not store a large raw JSON as a permanent fixture.** If a test needs values from a
   recording, extract the **minimal, deterministic** subset into `tests/plugins/fixtures/…`
   *with provenance* (source file name + sha256 + note) and a builder in
   `tests/plugins/fixtures/build_*_fixture.py` that refuses when recordings disagree. Never
   invent values, never round them "for convenience".
5. **A test must not read the newest file in a recorded-run directory.** Point it at a fixture,
   or make the input an explicit path/pattern argument (`restore_p8.py`,
   `live_harness.newest_evidence`). Newest-by-mtime is a timestamp dependency in disguise and it
   silently changes what a test measures when someone re-runs a probe. Re-analyzing a *recorded*
   run is a separate, opt-in workflow: point `REAPER_MCP_EVIDENCE_SEARCH` (or
   `REAPER_MCP_RECOVERY_SEARCH`) at the research archive when you want it, and never make it a
   default root.
6. **Look for an existing prepared artifact before measuring again.** `plugin_prepare(mode="reuse")`
   first: a fresh sweep of one instrument cost 48,598 host calls / 96 s / 28 MB in P27, while a
   stored artifact served in 2.3 s.
7. **The repository keeps the tools, not their output.** Probes that regenerate a *product input*
   (host calibration, enum boundaries) or an open contract's instrument (`probe_fxguid.py`) stay;
   a probe written for one finished measurement does not (P33 archived 36 such scripts).
7. **`.gitignore` stays narrow.** Never add `*.json`, `*.py`, `tests/plugins/live/**` or any
   whole-directory rule to hide run output — fix the output path instead. A coarse rule classifies
   the *next* piece of evidence as disposable.
8. **Never re-add or un-track generated evidence ad hoc.** The P33 purge is done; adding such a
   file back (or removing another one from the tree/history) is an explicitly approved operation
   with the exact file list in hand — never a side effect of another task. The original history
   and the removed working-tree files are preserved in the P33 backup
   (`%LOCALAPPDATA%\reaper_reapy_mcp\repo_cleanup\p33_*/`).
9. **Never run live-write tests in a normal test pass.** The offline command is
   `py -3.11 -m pytest -q -p no:cacheprovider tests/plugins --ignore=tests/plugins/live`.
   Without `--ignore`, `tests/plugins/live/test_*.py` performs **real writes on the P18–P20
   workbench tracks** while REAPER is open (incident, `IMPLEMENTATION-STATUS.md` §33), and live
   probes mutate the open project.
10. **Runtime/prepared data is not run evidence.** `%LOCALAPPDATA%\reaper_reapy_mcp\{cache,discovery,calibration}`
    is machine-local and regenerable; never commit it, never copy it into the repo as a fixture
    without the extraction-with-provenance rule above.
11. **`composer_v1_2_lm_probe.md` is protected.** Do not read-modify it, move it, or add it to Git.
12. **Git writes belong to the owner.** Do not `git add/commit/push/reset/stash/checkout/restore/clean`,
    do not rewrite history, do not change the index — unless a task explicitly says so, and then
    only for the exact paths it names.

## Conventions worth keeping

- Offline tests must stay hermetic (no host, no `%LOCALAPPDATA%` dependence, no live state); live
  probes are scripts (`probe_*.py`, never collected) and live tests carry the `live_write` marker.
- Code comments and test docstrings are English; docs and journals are mixed English/Russian.
- Provider facts are data-driven: a changed behaviour-affecting fact moves
  `implementation_revision` (r13 since P30) and any profile-schema change moves the format version —
  a cache entry is only invalidated when the key actually moves.
- A defect fix lands with a regression test named after the round (`tests/plugins/test_pNN_*.py`),
  and the round's measurements go into `IMPLEMENTATION-STATUS.md` §NN.
