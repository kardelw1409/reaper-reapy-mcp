"""Universal FX parameter discovery engine (P26).

A generic pipeline that inventories every parameter the host exposes for an FX
instance, collects the metadata the host API actually answers, samples the
host's own display formatter, recovers ranges and conversion shapes *from those
measurements*, classifies the controls, infers a probable module structure and
stores the result as a self-contained, re-checkable prepared artifact.

What this package is **not**:

- not a provider: it contains no plugin identifier, no registry and no
  hand-written per-plugin fact. Every decision is taken from host metadata plus
  the observations of one run (the boundary is enforced by the P7 isolation
  suite and by this round's `test_p26_generic_independence.py`);
- not a second tool layer: the tools stay `plugins/tools.py` + `plugins/api.py`
  and the prepared artifacts are addressed through the existing resolution and
  cache path;
- not a writer: the engine only *reads* the host (values, names, idents, step
  sizes, named config, formatted display). The one host call it uses that takes
  a value - ``TrackFX_FormatParamValueNormalized`` - formats a value the caller
  passes and never stores it (measured read-only in P22/P23 and re-proved here).

Module map (functional split, not a file count):

============================  ==================================================
``evidence``                  source kinds, strength, conflicts, unknown reasons
``text``                      display-text parsing and printed resolution
``types``                     the data the stages hand to each other
``inventory``                 host inventory: every visible parameter + metadata
``formats``                   format-adapter contract (VST3/CLAP/VST2/AU/…)
``observe``                   one run's observation store + sampling engine
``mapping``                   range and conversion-shape analyzer
``discrete``                  enum / toggle / ladder / ramp analysis
``classify``                  control classification from combined evidence
``modules``                   module and submodule inference
``pipeline``                  orchestration, budgets, timings, checkpoints
``report``                    human-readable statistics and disputed fields
``store``                     prepared artifacts (portable, not disposable)
``replay``                    offline replay + comparison on recorded data
============================  ==================================================
"""

from __future__ import annotations

__all__ = ["ENGINE_VERSION"]

#: Version of the discovery *algorithms*, recorded in every artifact and run
#: row. It moves whenever a decision rule changes meaning, so a stored prepared
#: artifact can be recognised as stale rather than silently re-used.
ENGINE_VERSION = "discovery-v1"
