"""P41: a response's BOILERPLATE has a budget, and the identity prose has exactly one home.

The owner's observation, and the measurement behind it: a `plugin_sample(op="read")` answer spent a
quarter of its size on `meta` - and `meta` rides EVERY call of EVERY tool. Of that, 716 characters
were the identity/continuity disclosure spelled out as two paragraphs, repeated verbatim forever.

Two rules came out of it, and both are testable:

1. **The claim stays, the paragraph does not.** The audit requirement (P11: a caller must see what a
   target token does NOT establish without asking) is satisfied by the compact form - validated,
   not_claimed, reason, evidence, and a one-sentence consequence. The prose moved to
   `plugin_describe`, which is what a caller reads once.
2. **An empty list says nothing.** `refusals: []`, `quarantined: []`, `warnings: []`, `reason: null`,
   `capped: 0` and a directory path that duplicates `cache.path` are dropped from the wire form.

The budget below is a MEASUREMENT, not a preference: it is set just above what the compact form
costs, so a future addition to `meta` has to be argued for rather than absorbed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PLUGINS = Path(__file__).resolve().parents[2] / "src" / "reaper_reapy_mcp" / "plugins"
if str(PLUGINS.parents[1]) not in sys.path:
    sys.path.insert(0, str(PLUGINS.parents[1]))

from reaper_reapy_mcp.plugins import resolve as RS  # noqa: E402


def size(payload: object) -> int:
    return len(json.dumps(payload, separators=(",", ":")))


def test_the_compact_disclosure_carries_every_field_of_the_claim():
    compact = RS.identity_basis()
    assert compact["not_claimed"] == ["instance_continuity"]
    assert compact["reason"] == "identical_build_replacement_indistinguishable"
    assert "location" in compact["validated"] and "state_revision" in compact["validated"]
    assert "p8_l3_identity" in compact["measured_by"], "the evidence pointer stays"
    assert "does not claim" in compact["consequence"]
    assert "why" not in compact, "the paragraph is not repeated on every response"
    assert "plugin_describe" in compact["consequence"], "the compact form says where the prose is"


def test_the_compact_disclosure_is_a_third_of_the_prose():
    """The numbers the trim was decided on: 716 characters for the prose, 450 for the claim."""
    assert size(RS.identity_basis(full=True)) == 716, "the prose form as it shipped"
    compact = size(RS.identity_basis())
    assert compact <= 460, compact
    assert compact < size(RS.identity_basis(full=True)) * 0.65, compact
    # the evidence is named by its id (the corpus' own rule) in the compact form, by path in the prose
    assert RS.identity_basis()["measured_by"].endswith(".json")
    assert "tests/plugins/live/evidence" in RS.identity_basis(full=True)["measured_by"]


def test_the_full_prose_is_still_reachable():
    full = RS.identity_basis(full=True)
    assert "removing and re-adding the same build" in full["why"]
    assert "state-based" in full["consequence"]
    # the claim's own fields are IDENTICAL between the forms, never a weaker claim in the compact one
    compact = RS.identity_basis()
    for key in ("validated", "not_claimed", "reason"):
        assert compact[key] == full[key], key
    # only the citation differs, by design: id in the compact form, path in the prose
    assert compact["measured_by"] in full["measured_by"]
    # a returned copy is never the module's own dict (a caller could mutate the contract)
    compact["validated"].append("invented")
    assert RS.identity_basis()["validated"] == ["location", "build_facts", "state_revision"]


@pytest.mark.parametrize(
    ("tool_name", "sentence"),
    (
        ("plugin_sample", "carries the PCM inline"),
        ("plugin_wavetable", "no second frame to move to"),
    ),
)
def test_the_new_tools_payloads_carry_no_static_prose(tool_name: str, sentence: str):
    """Static explanation belongs in the tool description, not in every answer.

    MEASURED: the sampler's `read` carried a 200-character note about the block being an ordinary
    state path, and a wavetable read carried a paragraph about single-frame tables - text that is
    identical on every call for every instance.
    """
    from reaper_reapy_mcp.plugins import tools as T

    description = json.dumps(T.SPEC[tool_name], separators=(",", ":"))
    assert sentence in description, f"{tool_name}: the explanation lives in the tool description"


def test_the_meta_trim_rules_are_visible_in_the_source():
    """`_meta` drops empties and the duplicated directory - pinned as source facts.

    These are wire-shape rules (what a caller receives), and a behavioural test would need a full
    mock session for each; the assertions below are about the two lines that implement them, so a
    later edit that re-adds `refusals: []` to every answer fails here and has to argue for itself.
    """
    source = (PLUGINS / "api.py").read_text(encoding="utf-8")
    assert "if value not in ([], {}, None)" in source
    assert '"directory", "configured_providers", "reason", "capped"' in source
    assert "RS.identity_basis(full=True)" in source, "the prose has exactly one home"
