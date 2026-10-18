"""P43 - the plugin rewrites what it was given, and that is NOT a foreign change (measured live).

MEASURED, live, through the product surface (2026-10-18, `plugin_presets(op="apply")`):

    the preset's `lfos[0].points` = [0,1, 0,0, 1,1, 1,1]  (two points share x = 1)
    the write is accepted; the read-back is the PLUGIN's own form:
        points [0,1, 0,0, 1,1, 0,0], powers [0,0,0,0]
    1 unit of 14 mismatched -> WriteNotApplied, unit 13
    the rollback then DID NOT RUN:
        outcome.verdict = FAILED_AND_NOT_RESTORED, final_state: unknown,
        named_gaps: state + parameters "not_restored (external_change)"

The rule that skipped it is right in spirit - never restore over state that is not ours - but the
state the plugin held WAS ours: it was our own written document, re-serialized by the plugin when
it loaded it. The layer had recorded only the bytes it SENT, so the plugin's rendering looked
foreign, and a deterministic failure (a repeat dry run plans the same write) became a dead end -
the same class as the P42 refusal whose printed remedy could not work.

The fix recognises the state on evidence, not on faith: the observed document must differ from the
INTENDED one only inside the paths THIS unit wrote (the codec's own differ over the space it
writes). Anything outside our paths keeps the rule's old answer - that is the control this file
also pins. The parameter channel rides along on the same evidence, because a state write resets
ordinary parameters (measured), which is exactly what moved the vector in the incident.

Mock host + fixtures only: no live REAPER.
"""

from __future__ import annotations

import base64
import copy
import sys
import types
from pathlib import Path

import pytest

PLUGINS = Path(__file__).resolve().parents[2] / "src" / "reaper_reapy_mcp" / "plugins"
if str(PLUGINS.parents[1]) not in sys.path:
    sys.path.insert(0, str(PLUGINS.parents[1]))

import mock_host as MH  # noqa: E402

from reaper_reapy_mcp.plugins import model as M  # noqa: E402
from reaper_reapy_mcp.plugins import transaction as TR  # noqa: E402
from reaper_reapy_mcp.plugins.providers.vital import codec as VC  # noqa: E402
from reaper_reapy_mcp.plugins.resolve import TargetTokenRegistry  # noqa: E402

import test_p7_vital as T7  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

LFO_PATH = "settings.lfos.0.points"
#: The preset's curve: a duplicate x (two points at 1.0), which the plugin canonicalises on load.
REQUESTED = [0.0, 1.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0]
#: What the live read-back held instead - the measured rendering.
CANONICAL = [0.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0]
#: A path this write never touches: a change there is genuinely foreign.
FOREIGN_PATH = "settings.filter_1_cutoff"


def document_with_lfo() -> dict:
    """The fixture document plus the element the incident turned on."""
    doc = T7.document()
    doc["settings"]["lfos"] = [
        {"num_points": 3, "points": [0.0, 0.0, 1.0, 0.5], "powers": [0.0, 0.0]}
    ]
    return doc


def reserializing_transform(*, foreign: bool = False):
    """The plugin's OWN re-serialization: the exact non-canonical curve comes back canonicalised.

    `foreign=True` adds the other thing a state can carry at that moment - a path this write never
    touched - which is the case the external-change rule exists for.
    """

    def transform(block_text: str) -> str:
        framing = VC.parse_block_framing(block_text)
        doc, _brace = VC.decode_document(framing["B"])
        if VC.dig(doc, LFO_PATH) == REQUESTED:
            VC.set_path(doc, LFO_PATH, list(CANONICAL))
            if foreign:
                VC.set_path(doc, FOREIGN_PATH, 99.0)
        new_b = VC.encode_item_b(framing["B"], doc, mode="grow")
        item_a = VC.encode_item_a(framing["A"], len(new_b))
        lines = [framing["header"], base64.b64encode(item_a).decode("ascii")]
        lines.extend(
            VC.wrap_lines(base64.b64encode(new_b).decode("ascii"), framing.get("b64_width") or 280)
        )
        if framing.get("b64_artifact"):
            lines.append(framing["b64_artifact"])
        lines.extend(framing["tail_lines"])
        return "\n".join(lines)

    return transform


def host_with(document: dict, *, transform=None, reset_params=None) -> MH.MockHostBridge:
    host = MH.MockHostBridge(
        tracks=[MH.MockTrack(0, "Track 1", [T7.vitals_slot(block=T7.build_block(document))])],
        state_write_transform=transform,
    )
    host.slot(0, 0).version = "1.5.5"
    # the MEASURED behaviour this incident's parameter channel showed: a state write resets
    # ordinary parameters (the transaction's own comment records it as measured)
    host.reset_params_on_state_write = reset_params
    return host


def decode_live(host: MH.MockHostBridge) -> dict:
    codec = T7.provider_on_fixtures().codec()
    ref = M.PluginInstanceRef(
        host="reaper", location={"track_index": 0, "fx_index": 0}, provider="vital", identity=T7.IDENTITY
    )
    return codec.decode(host.locate_state_slice(ref, host.fx_facts(0, 0), host.read_state_chunk(ref)))


def run_request(host: MH.MockHostBridge, request: dict):
    provider = T7.provider_on_fixtures()
    profile = provider.build_profile(dict(T7.IDENTITY))
    registry = TargetTokenRegistry()
    token = registry.issue(facts=host.fx_facts(0, 0))
    ref = M.PluginInstanceRef(
        host="reaper", location={"track_index": 0, "fx_index": 0}, provider="vital", identity=T7.IDENTITY
    )
    return TR.Transaction(
        provider=provider,
        codec=provider.codec(),
        bridge=host,
        profile=profile,
        ref=ref,
        request=request,
        token=token,
        registry=registry,
        dry_run=False,
    ).run()


def gaps_of(result) -> list[dict]:
    return list(result.outcome.named_gaps or [])


# ---------------------------------------------------------------------------
# The incident, closed
# ---------------------------------------------------------------------------


def test_a_reserialized_write_is_recognised_and_the_rollback_runs():
    """The live refusal, reproduced offline: refuses, AND the state comes back."""
    document = document_with_lfo()
    captured_points = list(document["settings"]["lfos"][0]["points"])
    captured_level = 0.25  # the mock's own value for `2:1002` (test_p7_vital.HOST_PARAMS)
    host = host_with(
        document,
        transform=reserializing_transform(),
        reset_params={"2:1002": 0.0},
    )
    result = run_request(host, {"state": {LFO_PATH: list(REQUESTED)}})

    # the refusal itself is unchanged: the write did not land, and it says which unit/path
    assert result.ok is False
    assert result.error is not None and result.error.code == "WriteNotApplied"
    assert result.error.detail["mismatched"][0]["path"] == LFO_PATH
    # the fix: the rollback RAN and verified, because the state was ours (re-serialized)
    assert result.outcome.verdict is M.Verdict.FAILED_AND_RESTORED, result.outcome.named_gaps
    recognised = [gap for gap in gaps_of(result) if gap.get("reason") == "plugin_reserialized_write"]
    assert recognised, gaps_of(result)
    assert all(
        path == LFO_PATH or path.startswith(f"{LFO_PATH}.") for path in recognised[0]["paths"]
    ), recognised[0]
    # and the captured state - document AND parameter vector - is back
    live = decode_live(host)
    assert live["document"]["settings"]["lfos"][0]["points"] == captured_points
    assert host.slot(0, 0).params["2:1002"] == captured_level


def test_a_foreign_change_outside_our_paths_keeps_the_old_refusal():
    """The control: the recognition must be able to say NO, or it proves nothing.

    The same canonicalisation PLUS one path this write never touched: that is not our state any
    more, and the layer leaves it alone (the failure mode the rule exists to prevent).
    """
    document = document_with_lfo()
    host = host_with(
        document,
        transform=reserializing_transform(foreign=True),
        reset_params={"2:1002": 0.0},
    )
    result = run_request(host, {"state": {LFO_PATH: list(REQUESTED)}})

    assert result.ok is False
    assert result.outcome.verdict is M.Verdict.FAILED_AND_NOT_RESTORED, result.outcome.named_gaps
    assert not [gap for gap in gaps_of(result) if gap.get("reason") == "plugin_reserialized_write"]
    reasons = " ".join(str(gap.get("reason")) for gap in gaps_of(result))
    assert "external_change" in reasons, gaps_of(result)
    # nothing was restored: the plugin's rendering of our write is still there, untouched
    live = decode_live(host)
    assert live["document"]["settings"]["lfos"][0]["points"] == CANONICAL
    assert live["document"]["settings"][FOREIGN_PATH.split(".", 1)[1]] == 99.0


def test_a_codec_without_the_hooks_gets_no_recognition():
    """A provider that cannot compute the comparison is never credited with it."""
    unit = types.SimpleNamespace(payload={"paths": {LFO_PATH: list(REQUESTED)}})
    stub = types.SimpleNamespace(codec=types.SimpleNamespace())
    assert TR.Transaction._plugin_reserialized(stub, unit, {}, {}) is None


# ---------------------------------------------------------------------------
# The differ itself
# ---------------------------------------------------------------------------


def test_the_differ_covers_the_writable_space_and_ignores_derived_leaves():
    codec = T7.provider_on_fixtures().codec()
    base = document_with_lfo()
    assert codec.differing_paths(base, copy.deepcopy(base)) == []

    changed = copy.deepcopy(base)
    changed["settings"]["lfos"][0]["points"] = list(CANONICAL)
    differing = codec.differing_paths(base, changed)
    # the differ speaks at LEAF granularity: the fixture's curve is [0,0,1,0.5] and CANONICAL is
    # [0,1,0,0,1,1,0,0], so index 0 agrees, indices 1..3 disagree and 4..7 exist on one side only
    fixture_points = base["settings"]["lfos"][0]["points"]
    expected = [
        f"{LFO_PATH}.{index}"
        for index in range(len(CANONICAL))
        if index >= len(fixture_points) or fixture_points[index] != CANONICAL[index]
    ]
    assert differing == expected, differing
    assert differing and all(
        path == LFO_PATH or path.startswith(f"{LFO_PATH}.") for path in differing
    ), differing
    assert codec.differing_paths(changed, base) == differing, "the differ is symmetric"

    # the plugin re-derives the sample payload on every load (its measured reload delay), so a
    # difference there can neither confirm nor deny a foreign change
    left = copy.deepcopy(base)
    right = copy.deepcopy(base)
    for doc, payload in ((left, "AAAA"), (right, "BBBB")):
        doc["settings"]["sample"] = {"name": "White Noise", "length": 44100, "samples": payload}
    assert codec.differing_paths(left, right) == []

    # a wavetable slot is ONE value, and a slot present on one side only IS a difference
    left["settings"]["wavetables"] = [
        {"groups": [{"components": [{"keyframes": [{"wave_data": "AAAA"}]}]}]}
    ]
    assert codec.differing_paths(left, right) == [f"{VC.MUTABLE_ROOT}.wavetables.0"]
    assert codec.differing_paths(right, left) == [f"{VC.MUTABLE_ROOT}.wavetables.0"]

    # labels are writable too (P38), so a label difference is a difference
    labelled = copy.deepcopy(base)
    labelled["author"] = "someone"
    assert codec.differing_paths(base, labelled) == ["author"]
