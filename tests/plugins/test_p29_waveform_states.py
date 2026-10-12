"""P29 stage D: a discrete state is chosen by a **measured** position, not a guess.

Two defects were measured live in P29 and are pinned here.

1. **The write target of an enumerated control was never verified.** The band map's
   read direction (position -> label) was checked against every recorded sample;
   the write direction (label -> position) used the arithmetic midpoint of the
   member's recorded band. On ReaComp's `Ratio` - whose bands come from a 17-point
   grid - that midpoint lies *between* two samples, and the plugin printed
   `8.73 :1` after a request for `7.19 :1`: a **different member than the one that
   was named**. The target is now the midpoint of the positions that actually
   printed the label (a single one is used as itself), and both directions are
   verified before a law is offered.

2. **A labelled state of an otherwise numeric control could not be written at
   all.** Surge's oscillator volumes print `-inf dB`, `-72.00 dB`, ... - a ladder
   whose minimum is not a number, so a numeric law cannot express it and a string
   request was refused with "expected a number". The labels are recorded evidence,
   so they are carried as `labelled_states` **with the position they were measured
   at**, and a request naming one writes that position.
"""

from __future__ import annotations

import pytest

from reaper_reapy_mcp.plugins import host_scope as HS
from reaper_reapy_mcp.plugins import request_value as RV
from reaper_reapy_mcp.plugins.discovery.store import PreparedArtifact
from reaper_reapy_mcp.plugins.errors import InvalidValue

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# Where a label was measured
# ---------------------------------------------------------------------------


def test_a_label_measured_once_is_written_at_that_exact_position():
    """The live ReaComp `Ratio` shape: each member's band is a sampled point plus an
    extrapolated neighbour, so the band's midpoint is *not* a measured position."""
    samples = [(0.0, "1.000 :1"), (0.0625, "7.19 :1"), (0.125, "13.4 :1")]
    members = [
        {"id": "1.000 :1", "label": "1.000 :1", "band": [0.0, 0.03125]},
        {"id": "7.19 :1", "label": "7.19 :1", "band": [0.0625, 0.09375]},
        {"id": "13.4 :1", "label": "13.4 :1", "band": [0.125, 0.15625]},
    ]
    targets = HS.label_positions(samples, members)
    assert targets["7.19 :1"]["position"] == 0.0625, "NOT the 0.078125 band midpoint"
    assert targets["7.19 :1"]["basis"] == "measured"
    assert targets["13.4 :1"]["position"] == 0.125


def test_a_label_measured_twice_is_written_between_its_measurements():
    samples = [(0.0, "A"), (0.0625, "B"), (0.125, "B"), (0.1875, "C")]
    members = [
        {"id": "A", "label": "A", "band": [0.0, 0.03125]},
        {"id": "B", "label": "B", "band": [0.0625, 0.125]},
        {"id": "C", "label": "C", "band": [0.1875, 0.21875]},
    ]
    target = HS.label_positions(samples, members)["B"]
    assert target["position"] == pytest.approx(0.09375)
    assert target["basis"] == "measured_bracketed"
    assert target["bracketed_by"] == [0.0625, 0.125]


def test_a_label_nothing_printed_falls_back_to_its_recorded_band_start():
    samples = [(0.0, "A"), (0.0625, "B")]
    members = [
        {"id": "A", "label": "A", "band": [0.0, 0.03125]},
        {"id": "B", "label": "B", "band": [0.0625, 0.09375]},
        {"id": "Ghost", "label": "Ghost", "band": [1.0, 1.0]},
    ]
    target = HS.label_positions(samples, members)["Ghost"]
    assert target["position"] == 1.0
    assert target["basis"] == "band_first"


# ---------------------------------------------------------------------------
# The law: both directions verified
# ---------------------------------------------------------------------------


def _enum_artifact(bands: list[list[float]], labels: list[str], samples: list[tuple[float, str]]) -> dict:
    return {
        "kind": "fx-discovery",
        "schema_version": 1,
        "engine_version": "discovery-v1",
        "mode": "prepare",
        "identity": {"fx_name": "Fake", "class_id": "x", "module": "m", "param_count": 1},
        "payload": {
            "counts": {"controls_analyzed": 1},
            "cost": {"total_calls": 0},
            "modules": {"nodes": [], "signals_used": ["name"]},
            "controls": [
                {
                    "index": 0,
                    "name": "Mode",
                    "ident": "0:1",
                    "module": "mode",
                    "classification": {"kind": "enum", "confidence": "medium", "reasons": ["labels"]},
                    "range": {"reason": "api_does_not_expose"},
                    "mapping": {"fits": [], "reason": "insufficient_points"},
                    "discrete": {
                        "family": "enum",
                        "state": "sampled_upper_bounds",
                        "members": [
                            {"ordinal": index, "label": label, "band": band}
                            for index, (label, band) in enumerate(zip(labels, bands))
                        ],
                    },
                    "steps": {"reported": False},
                }
            ],
        },
        "observations": [
            {"index": 0, "samples": [{"normalized": position, "text": text} for position, text in samples]}
        ],
    }


def _materialize(document: dict, path: str) -> dict:
    artifact = PreparedArtifact(document=document, status="ok", path=path)
    return HS.materialize_profile(artifact)


def test_the_write_target_is_on_a_measured_position_and_reads_back_as_its_member():
    document = _enum_artifact(
        bands=[[0.0, 0.03125], [0.0625, 0.09375], [0.125, 0.15625]],
        labels=["1.000 :1", "7.19 :1", "13.4 :1"],
        samples=[(0.0, "1.000 :1"), (0.0625, "7.19 :1"), (0.125, "13.4 :1")],
    )
    profile = _materialize(document, "memory:enum-targets-a")
    control = profile["controls"][0]
    law = control["law"]
    assert law["kind"] == "enum_index"
    assert law["params"]["positions"] == [0.0, 0.0625, 0.125], "no extrapolated midpoint"
    assert law["params"]["position_basis"] == ["measured"] * 3
    evidence = control["evidence"]["law_evidence"]
    assert evidence["ok"] is True
    assert evidence["write_targets_wrong"] == []
    assert all(row["agrees"] for row in evidence["write_targets"])


def test_a_law_whose_write_target_lands_in_another_member_is_refused():
    """The honest answer when the recorded bands cannot be trusted to invert: no law
    at all, rather than one that writes a member the caller did not name."""
    document = _enum_artifact(
        bands=[[0.0, 0.5], [0.5, 0.75], [0.75, 1.0]],
        labels=["Alpha", "Beta", "Gamma"],
        samples=[(0.0, "Alpha"), (0.5, "Beta"), (1.0, "Gamma")],
    )
    profile = _materialize(document, "memory:enum-targets-b")
    control = profile["controls"][0]
    # Alpha's own measured position (0.0) sits in Alpha's band, Beta's in Beta's -
    # so this control DOES verify, which is the control case for the next assertion
    assert control["law"]["kind"] == "enum_index"

    # now a member's band excludes the very position that printed its label, so the
    # inversion sends that target to a *different* member
    document["payload"]["controls"][0]["discrete"]["members"] = [
        {"ordinal": 0, "label": "Alpha", "band": [0.0, 0.55]},
        {"ordinal": 1, "label": "Beta", "band": [0.6, 0.75]},
        {"ordinal": 2, "label": "Gamma", "band": [0.8, 1.0]},
    ]
    document["content_sha256"] = "shifted"
    shifted = _materialize(document, "memory:enum-targets-c")
    row = shifted["controls"][0]
    wrong = row["evidence"]["law_evidence"]["write_targets_wrong"]
    assert row["law"]["kind"] == "none", wrong
    assert [entry["member"] for entry in wrong] == ["Beta"]
    assert row["evidence"]["law_reason"] == "band_map_disagrees_with_samples"


# ---------------------------------------------------------------------------
# A labelled state on a numeric control
# ---------------------------------------------------------------------------


def test_a_numeric_control_keeps_its_measured_labels():
    """Surge's oscillator volumes: a `table` law (the numbers are monotone) whose
    minimum prints `-inf dB` - so the ladder's labels are carried beside the law."""
    document = {
        "kind": "fx-discovery",
        "schema_version": 1,
        "engine_version": "discovery-v1",
        "mode": "prepare",
        "identity": {"fx_name": "Fake", "class_id": "x", "module": "m", "param_count": 1},
        "payload": {
            "counts": {"controls_analyzed": 1},
            "cost": {"total_calls": 0},
            "modules": {"nodes": [], "signals_used": ["name"]},
            "controls": [
                {
                    "index": 0,
                    "name": "A Osc 1 Volume",
                    "ident": "0:1",
                    "module": "osc",
                    "classification": {"kind": "enum", "confidence": "medium", "reasons": ["labels"]},
                    "range": {"chosen": [-72.0, 0.0], "space": "display"},
                    "mapping": {"fits": [], "reason": "insufficient_points"},
                    "discrete": {
                        "family": "enum",
                        "state": "sampled_upper_bounds",
                        "members": [
                            {"ordinal": 0, "label": "-inf dB", "band": [0.0, 0.03125]},
                            {"ordinal": 1, "label": "-72.00 dB", "band": [0.0625, 0.09375]},
                            {"ordinal": 2, "label": "0.00 dB", "band": [1.0, 1.0]},
                        ],
                    },
                    "steps": {"reported": False},
                }
            ],
        },
        "observations": [
            {
                "index": 0,
                "samples": [
                    {"normalized": 0.0, "text": "-inf dB"},
                    {"normalized": 0.0625, "text": "-72.00 dB"},
                    {"normalized": 0.5, "text": "-18.00 dB"},
                    {"normalized": 1.0, "text": "0.00 dB"},
                ],
            }
        ],
    }
    profile = _materialize(document, "memory:labelled-states")
    control = profile["controls"][0]
    states = {state["id"]: state for state in control["labelled_states"]}
    assert set(states) == {"-inf dB", "-72.00 dB", "0.00 dB"}
    assert states["-inf dB"]["position"] == 0.0
    assert states["-inf dB"]["basis"] == "measured"

    # a request naming a label is honoured at the position it was measured at
    row = RV.resolve_control_value(
        {**control, "authority": {"write": "parameter"}}, "-inf dB", values_module=__import__(
            "reaper_reapy_mcp.plugins.values", fromlist=["values"]
        )
    )["row"]
    assert row["normalized"] == 0.0
    assert row["display"] == "-inf dB"
    assert row["display_claim"]["reason"] == "labelled_state"
    assert row["value_space"] == "display"


def test_an_unknown_state_names_the_options_instead_of_the_type_error():
    document = {
        "kind": "fx-discovery",
        "schema_version": 1,
        "engine_version": "discovery-v1",
        "mode": "prepare",
        "identity": {"fx_name": "Fake", "class_id": "x", "module": "m", "param_count": 1},
        "payload": {
            "counts": {"controls_analyzed": 1},
            "cost": {"total_calls": 0},
            "modules": {"nodes": [], "signals_used": ["name"]},
            "controls": [
                {
                    "index": 0,
                    "name": "Volume",
                    "ident": "0:1",
                    "module": "osc",
                    "classification": {"kind": "enum", "confidence": "medium", "reasons": ["labels"]},
                    "range": {"chosen": [-72.0, 0.0], "space": "display"},
                    "mapping": {"fits": [], "reason": "insufficient_points"},
                    "discrete": {
                        "family": "enum",
                        "state": "sampled_upper_bounds",
                        "members": [
                            {"ordinal": 0, "label": "-inf dB", "band": [0.0, 0.03125]},
                            {"ordinal": 1, "label": "0.00 dB", "band": [1.0, 1.0]},
                        ],
                    },
                    "steps": {"reported": False},
                }
            ],
        },
        "observations": [
            {"index": 0, "samples": [
                {"normalized": 0.0, "text": "-inf dB"},
                {"normalized": 0.5, "text": "-36.00 dB"},
                {"normalized": 1.0, "text": "0.00 dB"},
            ]}
        ],
    }
    from reaper_reapy_mcp.plugins import values as V

    control = _materialize(document, "memory:labelled-states-b")["controls"][0]
    assert control["labelled_states"], "the labels are recorded"
    problem = RV.resolve_control_value(
        {**control, "authority": {"write": "parameter"}}, "-99 dB", values_module=V
    )["problem"]
    assert problem["code"] == "UnsupportedControl"
    assert problem["detail"]["reason"] == "unknown_labelled_state"
    assert "-inf dB" in problem["detail"]["states"]


def test_the_find_rows_carry_the_labels_so_a_state_can_be_chosen(tmp_path, monkeypatch):
    """A count says "this is a chooser"; only the words let a caller pick a state -
    which is how the waveform for an 808 gets *chosen* rather than inferred."""
    import asyncio
    import json

    from mcp.server.fastmcp import FastMCP

    from reaper_reapy_mcp.plugins import api as A
    from reaper_reapy_mcp.plugins import tools as T
    from reaper_reapy_mcp.plugins.discovery.index import load_index

    payload = _enum_artifact(
        bands=[[0.0, 0.03125], [0.0625, 0.15625], [0.1875, 0.21875]],
        labels=["Classic", "Sine", "Wavetable"],
        samples=[(0.0, "Classic"), (0.0625, "Sine"), (0.125, "Sine"), (0.1875, "Wavetable")],
    )["payload"]
    index = load_index(payload)
    found = index.find(["mode"])
    assert found["matches"], "the control must be findable"
    row = found["matches"][0]
    assert row["enum_labels"] == ["Classic", "Sine", "Wavetable"]
    assert row["members"] == 3, "the count stays for compatibility"
