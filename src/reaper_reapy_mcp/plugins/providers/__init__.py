"""Provider packages (08 §6-§7).

A provider package is the ONLY place provider identifiers may appear, and the
only place where a provider's document format is understood. The rules that
make that true mechanically:

- a provider package may **not** import `reapy` / `reascript_api` (the bridge is
  the only host importer inside `plugins/`, 08 §8.2);
- a provider package may **not** import `_b4_lib` / `_bh_session` /
  `ReaperController` or any other legacy module, and importing it must neither
  change the working directory nor read a registry file at import time (AUD-005,
  enforced by the recursive import-boundary test);
- host I/O reaches a provider only through the `HostBridge` object Core passes
  it; provider configuration is read on first use, never at import.
"""
