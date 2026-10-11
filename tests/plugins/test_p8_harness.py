"""P8 harness tests (offline, mock host): the harness must be able to refuse.

The live suites depend on this module's promises, so the promises are tested
where they can be tested deterministically - with the mock host:

- the target resolution refuses on **zero or several** candidates instead of
  picking one;
- an incomplete capture is reported as `not_captured` (the caller's refusal);
- the parameter comparison treats a *missing* ident as a difference, never as a
  zero (the tri-state trap this project has already been bitten by);
- the restore read-back resolves **idents to indices** through the live table:
  the first version read the values positionally and reported a genuine live
  restore as a failure, which is exactly the kind of defect that must not reach a
  live run twice.
"""

from __future__ import annotations

import fixtures as F
import live_harness as H
import mock_host as MH
from mock_provider import MockProvider

from reaper_reapy_mcp.plugins.errors import InstanceNotFound
from reaper_reapy_mcp.plugins.model import PluginInstanceRef


def host_with(*slots: MH.MockFxSlot) -> MH.MockHostBridge:
    return MH.MockHostBridge(tracks=[MH.MockTrack(0, "Track 1", list(slots) or [MH.MockFxSlot()])])


def test_document_leaves_diff_reports_declared_derived_leaves_separately():
    """A plugin-derived leaf is compared, named, and never counted as a mismatch."""
    snapshot = {"document": F.demo_document(), "root": "settings"}
    live = F.demo_document()
    live["settings"]["hq"] = False  # a writable leaf that moved
    live["settings"]["mode"] = "auto"  # declared derived, moved by the plugin
    outcome = H.document_leaves_diff(
        live, snapshot, declared_derived=["settings.mode"]
    )
    assert not outcome["all_equal"]
    assert [row["path"] for row in outcome["mismatched"]] == ["settings.hq"]
    assert outcome["declared_derived"] == ["settings.mode"]
    assert outcome["derived_compared"] == 1
    assert outcome["n_derived_mismatched"] == 1
    assert outcome["derived_mismatched"][0]["path"] == "settings.mode"
    assert outcome["compared"] == 1, "only the writable leaves are counted"

    stable = H.document_leaves_diff(F.demo_document(), snapshot, declared_derived=["settings.mode"])
    assert stable["all_equal"] and stable["n_derived_mismatched"] == 0


def test_describe_difference_locates_the_difference_and_decodes_base64():
    same = H.describe_difference("abc", "abc")
    assert same["type"] == "string" and same["captured_chars"] == 3
    different = H.describe_difference("abcdef", "abXdef")
    assert different["first_difference_index"] == 2
    import base64

    a = base64.b64encode(b"\x00\x00\x00\x00ABCD").decode()
    b = base64.b64encode(b"\x00\x00\x00\x00\x11\x11\x22\x22").decode()
    described = H.describe_difference(a, b)
    assert described["base64"]["identical"] is False
    assert described["base64"]["captured_leading_zero_bytes"] == 4
    assert described["base64"]["observed_leading_zero_bytes"] == 4
    assert H.describe_difference("not base64!!!", "also not").get("base64") is None


def test_resolve_target_refuses_zero_and_several_candidates():
    provider = MockProvider()
    empty = MH.MockHostBridge(tracks=[MH.MockTrack(0, "Track 1", [])])
    try:
        H.resolve_target(empty, provider)
    except InstanceNotFound as exc:
        assert exc.detail["provider"] == "demo"
    else:  # pragma: no cover - the refusal must happen
        raise AssertionError("no candidate must refuse")

    two = host_with(MH.MockFxSlot(), MH.MockFxSlot())
    try:
        H.resolve_target(two, provider)
    except InstanceNotFound as exc:
        assert len(exc.detail["candidates"]) == 2
    else:  # pragma: no cover
        raise AssertionError("two identical candidates must refuse, never pick")

    one = host_with(MH.MockFxSlot())
    target = H.resolve_target(one, provider)
    assert target.ref.location == {"track_index": 0, "fx_index": 0}
    assert target.token.id.startswith("tok_")


def test_capture_baseline_is_complete_and_names_what_it_could_not_capture():
    provider = MockProvider()
    host = host_with(MH.MockFxSlot())
    target = H.resolve_target(host, provider)
    baseline = H.capture_baseline(host, target)
    assert baseline.complete, baseline.not_captured
    assert len(baseline.parameters) == len(F.DEMO_PARAMS)
    assert baseline.binding_report["bound"] == 4
    assert baseline.revision.digest
    assert baseline.block_digest

    short = host_with(MH.MockFxSlot())
    short.inject("read_params", "store-nothing")
    target2 = H.resolve_target(short, provider)
    baseline2 = H.capture_baseline(short, target2)
    assert not baseline2.complete
    assert "parameters" in baseline2.not_captured


def test_parameter_comparison_treats_a_missing_ident_as_a_difference():
    outcome = H.compare_parameters({"3:1003": 0.5, "4:1004": 0.5}, {"3:1003": 0.5})
    assert not outcome["all_equal"]
    assert outcome["missing"] == 1
    assert outcome["mismatched"][0]["ident"] == "4:1004"

    within = H.compare_parameters({"3:1003": 0.5}, {"3:1003": 0.5004}, tolerance=0.001)
    assert within["all_equal"]
    outside = H.compare_parameters({"3:1003": 0.5}, {"3:1003": 0.51}, tolerance=0.001)
    assert not outside["all_equal"]


def test_restore_parameters_resolves_idents_through_the_live_table():
    provider = MockProvider()
    host = host_with(MH.MockFxSlot())
    target = H.resolve_target(host, provider)
    slot = host.slot(0, 0)
    # move two parameters and restore them **by ident**: index 6 is the last one,
    # so a positional read-back (indices 0..N-1) cannot accidentally look right
    slot.params["3:1003"] = 0.9
    slot.params["6:1006"] = 0.9
    captured = {"3:1003": 0.5, "6:1006": 0.25}
    outcome = H.restore_parameters(host, target.ref, captured)
    assert outcome["restored"], outcome["comparison"]
    assert outcome["comparison"]["ident_not_in_table"] == []
    assert slot.params["3:1003"] == 0.5 and slot.params["6:1006"] == 0.25
    assert outcome["write_calls"] == 1, "one batched hop per 256 pairs"

    # a value that is genuinely restored must not be reported as a failure, and a
    # value that is NOT restored must not be reported as one
    slot.params["3:1003"] = 0.777
    host.inject("write_params", "store-nothing")
    outcome2 = H.restore_parameters(host, target.ref, {"3:1003": 0.5})
    assert not outcome2["restored"]
    assert outcome2["comparison"]["mismatched"][0]["observed"] == 0.777


def test_document_leaves_diff_compares_the_captured_leaves():
    document = F.demo_document()
    snapshot = {"document": F.demo_document(), "root": "settings"}
    same = H.document_leaves_diff(document, snapshot)
    assert same["all_equal"] and same["compared"] > 0 and same["n_mismatched"] == 0

    changed = F.demo_document()
    changed["settings"]["hq"] = False
    outcome = H.document_leaves_diff(changed, snapshot)
    assert not outcome["all_equal"]
    assert outcome["mismatched"] == [
        {"path": "settings.hq", "captured": True, "observed": False}
    ]

    grown = F.demo_document()
    grown["settings"]["extra"] = 1
    outcome2 = H.document_leaves_diff(grown, snapshot)
    assert outcome2["all_equal"], "an added leaf is reported separately, not as a mismatch"
    assert outcome2["new_paths_not_captured"] == 1


def test_document_only_leaves_never_offer_a_route_table_leaf():
    """A live state-write case must not be aimed at the gate.

    The dependency gate refuses a state write under the route table that does not
    clear a pair field, so a harness offering such a leaf would drive the live case
    into a refusal and report it as a write failure. The filter is asserted in both
    directions: the route leaves are really there (non-vacuity) and the offered set
    does not overlap them.
    """
    from reaper_reapy_mcp.plugins.providers.vital import codec as VC

    document = F.demo_document()
    # the route table is written through the codec's own writer, so the fixture
    # contains what a real document contains (the demo template declares its routes
    # in its own shape, which is not the codec's)
    VC.make_routing(document, "src_a", "tone_cutoff", 0.5, slot=1)
    assert document["settings"][VC.ROUTE_TABLE_KEY], "the fixture must declare routes"

    routes = H.route_table_leaves(document)
    assert routes, "the fixture's route table must appear as leaves, or this proves nothing"
    assert all(path.startswith(f"settings.{VC.ROUTE_TABLE_KEY}") for path in routes), routes

    offered = H.document_only_leaves(
        H.resolve_target(host_with(MH.MockFxSlot()), MockProvider()).profile, document
    )
    assert offered, "the fixture must still offer a case, or the case would silently skip"
    assert not set(offered) & set(routes), (sorted(set(offered) & set(routes)), routes)
