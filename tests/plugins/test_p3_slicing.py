"""P3: target-aware slice location on multi-block fixtures (B-26..B-31).

Every LOCATOR case runs the SAME assertions against the production locator
(`plugins/host/reaper.py`) and the mock bridge's locator; if the two ever
diverge, the suite fails - that is the conformance guard for the read path.
The movement/replacement case (`test_b30...`) is token-level and uses the mock
host only.
"""

from __future__ import annotations

import pytest

import fixtures as F
import mock_host as MH

from reaper_reapy_mcp.plugins.errors import TargetSliceAmbiguous, Unreadable
from reaper_reapy_mcp.plugins.host import reaper as reaper_host
from reaper_reapy_mcp.plugins.model import text_digest

LOCATORS = [reaper_host.locate_block_span, MH.locate_block_span]
LOCATOR_IDS = ["production", "mock"]


def foreign_after() -> str:
    return F.foreign_block_text(
        name="VST3i: Third Synth", module="third.vst3", class_hex="22223333444455556666777788889999"
    )


def build_chunk() -> str:
    """foreign VST, the target, another foreign VST."""
    return F.track_chunk([F.foreign_block_text(), F.make_block_text(F.demo_document()), foreign_after()])


def demo(doc: dict | None = None, **kwargs) -> str:
    return F.make_block_text(doc or F.demo_document(), **kwargs)


# ---------------------------------------------------------------------------
# B-26 / B-27: the intended block is the one addressed, and only it changes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("locate", LOCATORS, ids=LOCATOR_IDS)
def test_b26_non_first_target_block_is_selected_exactly(locate):
    chunk = build_chunk()
    slice_ = locate(chunk, class_hex=F.DEMO_CLASS, module=F.DEMO_MODULE)
    assert slice_.line_start > 0, "the target is NOT the first block of the chunk"
    assert slice_.class_hex == F.DEMO_CLASS
    assert slice_.block_text == demo()
    assert slice_.is_valid_for(chunk)
    assert slice_.chunk_digest == text_digest(chunk)
    before, after = F.split_outside(chunk, slice_.line_start, slice_.line_end)
    assert F.foreign_block_text() in before and foreign_after() in after


@pytest.mark.parametrize("locate", LOCATORS, ids=LOCATOR_IDS)
def test_b27_preceding_foreign_block_is_never_the_target_or_the_document(locate):
    chunk = build_chunk()
    slice_ = locate(chunk, class_hex=F.DEMO_CLASS, module=F.DEMO_MODULE)
    assert F.DEMO_NAME in slice_.header_line
    assert F.FOREIGN_NAME not in slice_.header_line
    with pytest.raises(ValueError):
        F.decode_block_document(F.foreign_block_text())  # a foreign payload is not our document


@pytest.mark.parametrize("locate", LOCATORS, ids=LOCATOR_IDS)
def test_b26b_splicing_changes_only_the_selected_span(locate):
    chunk = build_chunk()
    slice_ = locate(chunk, class_hex=F.DEMO_CLASS, module=F.DEMO_MODULE)
    new_doc = F.demo_document()
    new_doc["settings"]["mode"] = "mono"
    replacement = demo(new_doc)
    lines = chunk.split("\n")
    spliced = "\n".join(
        lines[: slice_.line_start] + replacement.split("\n") + lines[slice_.line_end + 1 :]
    )
    before_b, after_b = F.split_outside(chunk, slice_.line_start, slice_.line_end)
    before_a, after_a = F.split_outside(spliced, slice_.line_start, slice_.line_end)
    assert (before_b, after_b) == (before_a, after_a)


# ---------------------------------------------------------------------------
# B-28 / B-29: duplicates need the ordinal; the module discriminates builds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("locate", LOCATORS, ids=LOCATOR_IDS)
def test_b28_two_identical_instances_need_a_consistent_ordinal(locate):
    second = demo(F.demo_document(), header=F.make_block_header())  # identical bytes
    chunk = F.track_chunk([F.foreign_block_text(), demo(), second])
    with pytest.raises(TargetSliceAmbiguous):
        locate(chunk, class_hex=F.DEMO_CLASS, module=F.DEMO_MODULE)  # no ordinal: refuse
    first = locate(chunk, class_hex=F.DEMO_CLASS, module=F.DEMO_MODULE, ordinal=0, matching_fx_count=2)
    other = locate(chunk, class_hex=F.DEMO_CLASS, module=F.DEMO_MODULE, ordinal=1, matching_fx_count=2)
    assert first.line_start < other.line_start
    assert first.ordinal == 0 and other.ordinal == 1
    with pytest.raises(TargetSliceAmbiguous):
        locate(chunk, class_hex=F.DEMO_CLASS, module=F.DEMO_MODULE, ordinal=2, matching_fx_count=2)


@pytest.mark.parametrize("locate", LOCATORS, ids=LOCATOR_IDS)
def test_b31_inconsistent_counts_refuse(locate):
    chunk = build_chunk()
    with pytest.raises(TargetSliceAmbiguous):
        locate(chunk, class_hex=F.DEMO_CLASS, module=F.DEMO_MODULE, ordinal=0, matching_fx_count=2)


@pytest.mark.parametrize("locate", LOCATORS, ids=LOCATOR_IDS)
def test_b29_module_string_discriminates_the_build(locate):
    vst2_twin = demo(F.demo_document(), name=F.DEMO_NAME, module="demo.dll", class_hex=F.DEMO_CLASS)
    chunk = F.track_chunk([vst2_twin, demo()])
    # the class id is identical for both builds: only the module separates them
    slice_ = locate(chunk, class_hex=F.DEMO_CLASS, module=F.DEMO_MODULE)
    assert slice_.line_start > 0 and "demo.vst3" in slice_.header_line
    # a class-id-only search cannot correlate uniquely
    with pytest.raises(TargetSliceAmbiguous):
        locate(chunk, class_hex=F.DEMO_CLASS, module=None)
    # and the module alone still finds the VST3 build
    vst3_only = locate(chunk, class_hex=None, module=F.DEMO_MODULE)
    assert "demo.vst3" in vst3_only.header_line


@pytest.mark.parametrize("locate", LOCATORS, ids=LOCATOR_IDS)
def test_missing_identity_evidence_refuses_instead_of_guessing(locate):
    chunk = build_chunk()
    with pytest.raises(TargetSliceAmbiguous):
        locate(chunk, class_hex=None, module=None)
    with pytest.raises(Unreadable):
        locate(chunk, class_hex="FF" * 16, module="absent.vst3")


# ---------------------------------------------------------------------------
# Byte fidelity details: CRLF chunks and the stale-slice rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("locate", LOCATORS, ids=LOCATOR_IDS)
def test_crlf_chunks_keep_their_outside_bytes(locate):
    chunk = F.track_chunk([F.foreign_block_text(), demo(), foreign_after()], newline="\r\n")
    slice_ = locate(chunk, class_hex=F.DEMO_CLASS, module=F.DEMO_MODULE)
    before, after = F.split_outside(chunk, slice_.line_start, slice_.line_end)
    assert "\r" in chunk and F.FOREIGN_NAME in before and "Third Synopsis" not in after
    replaced = "\n".join(
        chunk.split("\n")[: slice_.line_start]
        + demo(F.demo_document()).split("\n")
        + chunk.split("\n")[slice_.line_end + 1 :]
    )
    assert F.split_outside(replaced, slice_.line_start, slice_.line_end)[1] == after


@pytest.mark.parametrize("locate", LOCATORS, ids=LOCATOR_IDS)
def test_a_slice_is_only_valid_for_its_own_chunk(locate):
    chunk = build_chunk()
    slice_ = locate(chunk, class_hex=F.DEMO_CLASS, module=F.DEMO_MODULE)
    stale = chunk.replace('PEAKCOL 16576', 'PEAKCOL 16577')
    assert not slice_.is_valid_for(stale)


# ---------------------------------------------------------------------------
# B-30: movement / replacement is detected, never silently re-targeted
# ---------------------------------------------------------------------------


def test_b30_a_moved_or_replaced_target_is_not_silently_retargeted():
    from mock_provider import MockProvider

    from reaper_reapy_mcp.plugins import resolve as R
    from reaper_reapy_mcp.plugins.errors import InstanceNotFound, TargetChanged

    provider = MockProvider()
    registry = R.TargetTokenRegistry()
    host = MH.MockHostBridge(
        tracks=[MH.MockTrack(0, "Track 1", [MH.MockFxSlot(), MH.MockFxSlot(), MH.MockFxSlot()])]
    )
    resolved = R.resolve_one(
        host, provider, selector={"track_index": 0, "fx_index": 1}, registry=registry
    )
    assert resolved.ref.location == {"track_index": 0, "fx_index": 1}

    # (1) a different build appearing in the same slot is refused, never written to
    host.external_replace_slot(class_id="00000000000000000000000000000001", fx_index=1)
    with pytest.raises(TargetChanged) as excinfo:
        registry.revalidate(resolved.token.id, host.fx_facts(0, 1))
    assert "class_id" in excinfo.value.detail["fields"]

    # (2) a move shifts the addresses: the token no longer matches what sits at its location
    registry2 = R.TargetTokenRegistry()
    host2 = MH.MockHostBridge(
        tracks=[
            MH.MockTrack(
                0,
                "Track 1",
                [MH.MockFxSlot(), MH.MockFxSlot(version="1.0.1"), MH.MockFxSlot()],
            )
        ]
    )
    moved = R.resolve_one(host2, provider, selector={"fx_index": 1}, registry=registry2)
    assert moved.facts.version == "1.0.1"
    host2.external_move_slot(1, 0)
    with pytest.raises(TargetChanged) as moved_exc:
        registry2.revalidate(moved.token.id, host2.fx_facts(0, 1))
    assert "version" in moved_exc.value.detail["fields"]

    # (3) a selector that matches nothing is a typed refusal, never a guess
    with pytest.raises(InstanceNotFound):
        R.resolve_one(host2, provider, selector={"track_index": 5})
