"""P5: host write primitives and the State Codec contract - mocks and captured
chunk strings only. **No live mutation is performed or possible here** (the
conftest guard refuses a real-bridge write; see `test_p3_no_write.py`).

Covered: the splice byte-fidelity proof on multi-block chunks (LF and CRLF),
one conformance suite running unchanged over two codecs with different document
shapes and batching, the store-nothing regression, sequential-unit rebasing on
the observed read-back, and the write-attempt accounting on the transport path.

**Not live-certified:** the real bridge's write verbs are a tested abstraction
until P8 measures them against REAPER.
"""

from __future__ import annotations

import pytest

import fixtures as F
import mock_host as MH
from mock_codec_b import CodecB, b_document, make_b_block_text
from mock_provider import DemoCodec, MockProvider

from reaper_reapy_mcp.plugins import model as M
from reaper_reapy_mcp.plugins.host import reaper as R
from reaper_reapy_mcp.plugins.model import (
    FinalState,
    PluginInstanceRef,
    ResolvedRequest,
    TargetedStateSlice,
    Verdict,
    text_digest,
)
from reaper_reapy_mcp.plugins.resolve import TargetTokenRegistry
from reaper_reapy_mcp.plugins.transaction import Transaction

DEMO_MODULE_PATH = {"tree.engine.gain": 0.4, "flags.bypass": True}


# ---------------------------------------------------------------------------
# Scaffolding (independent of the P2 module's helpers on purpose)
# ---------------------------------------------------------------------------


def build_host() -> MH.MockHostBridge:
    foreign = MH.MockFxSlot(
        name=F.FOREIGN_NAME,
        module=F.FOREIGN_MODULE,
        class_id=F.FOREIGN_CLASS,
        block=F.foreign_block_text(),
        params={"0:9000": 0.1},
    )
    target = MH.MockFxSlot()
    sibling = MH.MockFxSlot(
        name="VST3i: Third Synth",
        module="third.vst3",
        class_id="99998888777766665555444433332222",
        block=F.foreign_block_text(
            name="VST3i: Third Synth", module="third.vst3", class_hex="99998888777766665555444433332222"
        ),
    )
    return MH.MockHostBridge(tracks=[MH.MockTrack(0, "Track 1", [foreign, target, sibling])])


def build_codec_b_host() -> MH.MockHostBridge:
    slot = MH.MockFxSlot(
        name="VST3b: Demo B",
        module="demo_b.vst3",
        class_id="FFEEDDCCBBAA99887766554433221100",
        block=make_b_block_text(F.flat_paths(b_document())),
    )
    return MH.MockHostBridge(tracks=[MH.MockTrack(0, "Track 1", [slot])])


class ProviderB(MockProvider):
    """Provider semantics over codec B's document shape."""

    def __init__(self) -> None:
        super().__init__()
        self._codec = CodecB()

    def normalize_request(self, profile, request):  # noqa: ANN001
        resolved = ResolvedRequest()
        for path, value in dict(request.get("state") or {}).items():
            resolved.state[str(path)] = value
        return resolved

    def codec(self) -> CodecB:
        return self._codec


def make(request, *, host=None, provider=None, dry_run=True, fx_index=1):
    host = host if host is not None else build_host()
    provider = provider or MockProvider()
    facts = host.fx_facts(0, fx_index)
    registry = TargetTokenRegistry(session_context={"project": "test"})
    token = registry.issue(facts=facts)
    ref = PluginInstanceRef(
        host="reaper",
        location={"track_index": 0, "fx_index": fx_index},
        provider=str(provider.id),
        identity=provider.identify(facts),
    )
    transaction = Transaction(
        provider=provider,
        codec=provider.codec(),
        bridge=host,
        profile=provider.build_profile(provider.identify(facts)),
        ref=ref,
        request=request,
        token=token,
        registry=registry,
        dry_run=dry_run,
    )
    return transaction, host


def assert_counters_consistent(result) -> None:
    accounting = result.outcome.host
    assert accounting.write_calls_attempted == (
        accounting.write_effects_verified + accounting.write_effects_unknown
    ), accounting.as_dict()
    assert result.outcome.mapping_is_consistent()


def slice_and_ref(host: MH.MockHostBridge, *, provider=None, fx_index: int = 1):
    provider = provider or MockProvider()
    facts = host.fx_facts(0, fx_index)
    ref = PluginInstanceRef(
        host="reaper",
        location={"track_index": 0, "fx_index": fx_index},
        provider=str(provider.id),
        identity=provider.identify(facts),
    )
    chunk = host.read_state_chunk(ref)
    return host.locate_state_slice(ref, facts, chunk), ref, facts, chunk


# ---------------------------------------------------------------------------
# P5-1: splice fidelity (the AUD-001 regression the POC got wrong)
# ---------------------------------------------------------------------------


def test_p5_1_splice_changes_only_the_selected_span():
    host = build_host()
    slice_, ref, facts, chunk = slice_and_ref(host)
    assert slice_.ordinal == 0
    before, after = F.split_outside(chunk, slice_.line_start, slice_.line_end)
    assert before + "\n" + slice_.block_text + "\n" + after == chunk

    new_doc = F.demo_document()
    new_doc["settings"]["mode"] = "mono"
    new_text = F.make_block_text(new_doc, header=slice_.header_line)
    spliced = R.splice_block_text(chunk, slice_, new_text)

    # an independent expectation of the same bytes
    lines = chunk.splitlines(keepends=True)
    expected = (
        "".join(lines[: slice_.line_start])
        + "\n".join(new_text.split("\n"))
        + "\n"
        + "".join(lines[slice_.line_end + 1 :])
    )
    assert spliced == expected

    # every differing byte lies inside the located span: the prefix before the
    # span and the suffix after it are copied verbatim (length-independent)
    span_start, span_end = F.char_span_of_lines(chunk, slice_.line_start, slice_.line_end)
    assert spliced[:span_start] == chunk[:span_start]
    suffix_len = len(chunk) - span_end
    assert spliced[len(spliced) - suffix_len :] == chunk[span_end:] if suffix_len else True
    assert spliced[span_start : len(spliced) - suffix_len if suffix_len else None] != chunk[
        span_start:span_end
    ], "the target span itself must differ"

    # the foreign block and the sibling block are byte-identical afterwards
    def blocks_by_class(text: str) -> dict[str, str]:
        return {block["class_hex"]: block["block_text"] for block in R.parse_blocks(text)}

    before_blocks = blocks_by_class(chunk)
    after_blocks = blocks_by_class(spliced)
    assert after_blocks[F.FOREIGN_CLASS] == before_blocks[F.FOREIGN_CLASS]
    assert after_blocks["99998888777766665555444433332222"] == before_blocks[
        "99998888777766665555444433332222"
    ]
    assert after_blocks[F.DEMO_CLASS] != before_blocks[F.DEMO_CLASS]
    # and the spliced target decodes to the new document
    assert F.decode_block_document(after_blocks[F.DEMO_CLASS])["settings"]["mode"] == "mono"


def test_p5_1b_crlf_chunk_keeps_its_line_endings():
    foreign = F.foreign_block_text()
    target_block = F.make_block_text(F.demo_document())
    chunk = F.track_chunk([foreign, target_block], newline="\r\n")
    slice_ = R.locate_block_span(
        chunk, class_hex=F.DEMO_CLASS, module=F.DEMO_MODULE, ordinal=0, matching_fx_count=1
    )
    new_doc = F.demo_document()
    new_doc["settings"]["hq"] = False
    new_text = F.make_block_text(new_doc, header=slice_.header_line)
    spliced = R.splice_block_text(chunk, slice_, new_text)

    lines = chunk.splitlines(keepends=True)
    expected = (
        "".join(lines[: slice_.line_start])
        + "\r\n".join(new_text.split("\n"))
        + "\r\n"
        + "".join(lines[slice_.line_end + 1 :])
    )
    assert spliced == expected
    assert spliced.count("\r\n") == chunk.count("\r\n"), "the line count must not move"
    assert "\n" not in spliced.replace("\r\n", ""), "no bare LF may appear in a CRLF chunk"
    # the hosted bytes outside the span are untouched
    prefix_len = len("".join(lines[: slice_.line_start]))
    assert spliced[:prefix_len] == chunk[:prefix_len]
    assert spliced.endswith(chunk[-len("".join(lines[slice_.line_end + 1 :])) :])


def test_p5_1c_splice_refuses_a_span_outside_the_chunk():
    host = build_host()
    slice_, _ref, _facts, chunk = slice_and_ref(host)
    bogus = TargetedStateSlice(
        chunk_digest=slice_.chunk_digest,
        line_start=10_000,
        line_end=10_001,
        header_line=slice_.header_line,
        class_hex=slice_.class_hex,
        ordinal=0,
        block_text=slice_.block_text,
    )
    try:
        R.splice_block_text(chunk, bogus, "x")
    except Exception as exc:  # noqa: BLE001 - asserted by type name and code below
        assert type(exc).__name__ == "Unreadable"
        assert exc.detail["chunk_lines"] > 0
    else:  # pragma: no cover - the refusal must happen
        raise AssertionError("a span outside the chunk must refuse, not splice")


def _reaper_shaped_chunk(*blocks: str, newline: str = "\n", trailing_newline: bool = True) -> str:
    """A chunk shaped like the **live** one (P8, 2026-10-10).

    Live shape: the plugin block is followed by the track's own ``WAK`` and
    terminator lines, and the chunk's last byte is a newline (871 LF, no CR).
    The mock's own `F.track_chunk` never produces the trailing newline, which is
    why the P5 suite could not see the defect fixed below.
    """
    lines = ["<TRACK", 'NAME "Track 1"', "PEAKCOL 16576"]
    for block in blocks:
        lines.extend(block.split("\n"))
    lines.extend(["WAK 0 0", ">"])
    text = newline.join(lines)
    return text + newline if trailing_newline else text


def test_p5_1h_a_chunk_ending_with_a_newline_is_never_shortened():
    """Live-detected defect (P8): the splice dropped the chunk's final newline.

    `split_lines_keeping_endings` discarded the trailing empty element of a
    chunk ending with ``"\\n"``, so ``"".join(lines)`` was one character short
    and **every state write deleted a byte outside the target span** - the exact
    property AUD-001 promises. The production chunk's shape (not the mock's) is
    what exposed it; both line endings and both endings-of-chunk are pinned here.
    """
    block = F.make_block_text(F.demo_document())
    for newline in ("\n", "\r\n"):
        for trailing in (True, False):
            chunk = _reaper_shaped_chunk(block, newline=newline, trailing_newline=trailing)
            lines = R.split_lines_keeping_endings(chunk)
            assert "".join(lines) == chunk, "the splitter must rejoin to the chunk exactly"
            slice_ = R.locate_block_span(
                chunk,
                class_hex=F.DEMO_CLASS,
                module=F.DEMO_MODULE,
                ordinal=0,
                matching_fx_count=1,
            )
            assert R.splice_block_text(chunk, slice_, slice_.block_text) == chunk, (
                "a no-op splice must be byte-identical, whatever the chunk's ending"
            )


def test_p5_1i_the_mock_chunk_shape_cannot_exercise_the_trailing_newline_case():
    """Why P5 missed it: the mock's chunk never ends with a newline.

    Recorded rather than re-fixed: the mock host's own splice is `"\\n".join(...)`
    on `chunk.split("\\n")`, which round-trips by construction - so the fixture
    generator, not the mock, was the blind spot.
    """
    chunk = F.track_chunk([F.make_block_text(F.demo_document())])
    assert not chunk.endswith("\n"), "the mock's chunk shape changed: re-check the claim above"
    assert "".join(R.split_lines_keeping_endings(chunk)) == chunk


def test_p5_1d_mock_transport_leaves_sibling_blocks_byte_identical():
    host = build_host()
    transaction, host = make(
        request={"state": {"settings.hq": False}}, host=host, dry_run=False
    )
    before = R.parse_blocks(host.read_state_chunk(transaction.ref))
    result = transaction.run()
    assert result.ok, result.error
    after = R.parse_blocks(host.read_state_chunk(transaction.ref))
    assert [b["class_hex"] for b in before] == [b["class_hex"] for b in after]
    for old, new in zip(before, after):
        if old["class_hex"] == F.DEMO_CLASS:
            assert old["block_text"] != new["block_text"]
        else:
            assert old["block_text"] == new["block_text"], old["class_hex"]
    assert_counters_consistent(result)


# ---------------------------------------------------------------------------
# P5-2: one conformance suite, two codecs (AUD-004)
# ---------------------------------------------------------------------------


def _codec_contract(codec, document, changes, slice_, policy) -> dict:
    """The shape-independent codec contract, run unchanged over both codecs."""
    plan = codec.plan_state_writes(document, changes)
    assert plan.units, "a non-empty change set must produce at least one unit"
    assert [unit.order_index for unit in plan.units] == list(range(len(plan.units)))
    assert all(unit.kind == "state" for unit in plan.units)
    assert plan.verification_plan, "a plan without a verification plan is not an exchange"

    doc, current = document, slice_
    for unit in plan.units:
        new_slice = codec.encode_slice(doc, unit, current)
        assert (new_slice.line_start, new_slice.line_end) == (current.line_start, current.line_end)
        assert new_slice.chunk_digest == current.chunk_digest, "the slice stays bound to its chunk"
        decoded = codec.decode(new_slice)
        report = codec.verify(decoded, unit, policy)
        assert report.ok, report.mismatched
        assert report.rows, "verification without rows is not evidence"
        doc, current = decoded, new_slice

    snapshot = codec.snapshot_document(document)
    restore = codec.restore_changes(doc, snapshot)
    restored, cursor = doc, current
    for unit in codec.plan_state_writes(doc, restore).units:
        encoded = codec.encode_slice(restored, unit, cursor)
        restored, cursor = codec.decode(encoded), encoded
    return {
        "units": len(plan.units),
        "document_after": doc,
        "document_restored": restored,
        "original": document,
    }


def test_p5_2_codec_a_passes_the_shared_contract():
    host = build_host()
    slice_, _ref, _facts, _chunk = slice_and_ref(host)
    outcome = _codec_contract(
        DemoCodec(),
        F.demo_document(),
        {"paths": {"settings.mode": "mono", "settings.hq": False}},
        slice_,
        MockProvider().verification_policy(),
    )
    assert outcome["units"] == 1, "codec A batches every path change into one unit"
    assert F.flat_paths(outcome["document_restored"]) == F.flat_paths(outcome["original"])
    assert F.flat_paths(outcome["document_after"]) != F.flat_paths(outcome["original"]), (
        "non-vacuity: the change must have moved something before the restore proves it returns"
    )


def test_p5_2_codec_b_passes_the_same_contract_with_different_batching():
    host = build_codec_b_host()
    slice_, _ref, _facts, _chunk = slice_and_ref(host, provider=ProviderB(), fx_index=0)
    outcome = _codec_contract(
        CodecB(),
        b_document(),
        {"paths": dict(DEMO_MODULE_PATH)},
        slice_,
        ProviderB().verification_policy(),
    )
    assert outcome["units"] == 2, "codec B splits one logical change by section"
    assert F.flat_paths(outcome["document_restored"]) == F.flat_paths(outcome["original"])
    assert F.flat_paths(outcome["document_after"])["flags.bypass"] is True


def test_p5_2b_one_engine_suite_two_shapes_and_batchings():
    """The engine executes whatever ordered units a codec returns; the same
    request shape is impossible for both codecs, so each runs its own document
    through the same assertions."""
    expectations = {
        "codec_a": (MockProvider(), build_host(), {"state": {"settings.hq": False}}, 1, 1),
        "codec_b": (ProviderB(), build_codec_b_host(), {"state": {"flags.bypass": True}}, 0, 1),
    }
    for label, (provider, host, request, fx_index, expected_units) in expectations.items():
        transaction, host = make(
            request=request, host=host, provider=provider, dry_run=False, fx_index=fx_index
        )
        result = transaction.run()
        assert result.ok, (label, result.error)
        assert result.outcome.verdict is Verdict.ALL_VERIFIED
        writes = [row for row in host.log if row[0] == "write_state_slice"]
        assert len(writes) == expected_units, (label, host.log)
        assert result.outcome.host.write_calls_attempted == expected_units
        assert result.outcome.host.write_effects_verified == expected_units
        assert result.outcome.host.write_effects_unknown == 0
        assert_counters_consistent(result)


# ---------------------------------------------------------------------------
# P5-3: "API success" is never an effect
# ---------------------------------------------------------------------------


def test_p5_3_store_nothing_is_write_not_applied_and_never_counted_as_landed():
    """The P5-3 row: a host that returns success without storing must produce
    `WriteNotApplied` and the read-back evidence that the effect did not land.

    Counter note (recorded P2 semantics, re-verified here): the counters answer
    "was the outcome of this write established by a read-back?", not "did the
    value change?". A write whose read-back proves it did NOT land is therefore
    counted as established (`verified`) with the mismatch in the channel rows,
    while a write whose read-back never happened is `unknown`. The invariant
    that must hold in every case is `attempted == verified + unknown` and the
    absence of any landed-effect claim for the failed unit.
    """
    host = build_host()
    landed_target_block = host.slot(0, 1).block
    host.inject("write_state_slice", "store-nothing", times=1)
    transaction, host = make(request={"state": {"settings.hq": False}}, host=host, dry_run=False)
    result = transaction.run()

    assert result.error is not None and result.error.code == "WriteNotApplied", result.error
    assert result.outcome.verdict is Verdict.FAILED_AND_RESTORED
    assert result.outcome.host.write_effects_unknown == 0, (
        "the outcome WAS established (as not-landed); it is not unknown"
    )
    assert result.outcome.host.write_effects_verified == result.outcome.host.write_calls_attempted
    mismatched = result.outcome.channels["state"].mismatched
    assert mismatched, "the failed unit must leave read-back evidence, not a claim"
    assert mismatched[0]["agrees"] is False
    assert_counters_consistent(result)

    # and a write whose read-back never happened IS unknown (never verified)
    host2 = build_host()
    host2.inject("write_state_slice", "drop-after-return", times=1)
    transaction2, _host2 = make(
        request={"state": {"settings.hq": False}}, host=host2, dry_run=False
    )
    result2 = transaction2.run()
    assert result2.outcome.host.write_effects_verified < (
        result2.outcome.host.write_calls_attempted
    )
    assert result2.outcome.host.write_effects_unknown >= 1
    assert_counters_consistent(result2)

    # the restored end state is the captured bytes, verified - and the verdict
    # says exactly that
    assert result.outcome.final_state is FinalState.UNCHANGED_VERIFIED
    assert host.slot(0, 1).block == landed_target_block
    assert_counters_consistent(result)


def test_p5_transport_never_establishes_an_effect_itself():
    """The bridge records what the host returned; establishing the effect is
    Core's decode-and-compare (AUD-004)."""
    host = build_host()
    _slice, ref, _facts, _chunk = slice_and_ref(host)
    attempt = host.write_params(ref, [("3:1003", 0.25)])
    assert attempt.effects_verified is None
    assert attempt.returned is not None
    assert attempt.attempted_utc.endswith("Z")


# ---------------------------------------------------------------------------
# P5-4: sequential units rebase on the observed read-back
# ---------------------------------------------------------------------------


def test_p5_4_sequential_units_rebase_on_the_read_back():
    host = build_host()
    transaction, host = make(
        request={
            "routes_create": [
                {"source": "src_a", "destination": "tone_level"},
                {"source": "src_b", "destination": "tone_cutoff"},
            ]
        },
        host=host,
        dry_run=False,
    )
    result = transaction.run()
    assert result.ok, result.error
    assert result.outcome.verdict is Verdict.ALL_VERIFIED
    writes = [row for row in host.log if row[0] == "write_state_slice"]
    assert len(writes) == 2, "one route per state write (the declared structural limit)"

    slot, ref, facts, chunk = slice_and_ref(host)
    document = transaction.codec.decode(host.locate_state_slice(ref, facts, chunk))
    landed = [route for route in document["routes"] if route.get("destination")]
    assert sorted(route["slot"] for route in landed) == [1, 2, 3, 4], (
        "the second unit must be encoded against the read-back of the first, "
        "not against the pre-write document"
    )
    assert_counters_consistent(result)


def test_p5_4b_a_slice_whose_block_is_unchanged_is_re_located_not_refused():
    """MEASURED (2026-10-18, live): a changed TRACK is not a changed PLUGIN.

    Starting the transport rewrote the track's chunk while a transaction was walking its units, so
    a slice located a second earlier no longer matched its chunk. Refusing there failed the whole
    transaction AND its rollback (the same guard fired in the rollback write, `state: false`),
    which left a ballooned wavetable slot holding its stub - a broken instance caused by an edit
    the caller never asked for. The transport now re-locates the block by its identity facts and
    splices there, because what must hold is that the BLOCK still holds the bytes the plan was
    built from.
    """
    host = build_host()
    transaction, host = make(request={"state": {"settings.hq": False}}, host=host, dry_run=False)
    _slice, ref, facts, chunk = slice_and_ref(host)
    # the track moved under us: a SIBLING plugin on the same track changed, so the chunk differs
    # (and the target's span may shift) while the target's own block is untouched - the live case
    # was the transport starting mid-transaction
    foreign = host.slot(0, 0)
    foreign.block = foreign.block.replace("Pk9uUFFy", "Pk9uUFFz")
    assert "Pk9uUFFz" in foreign.block, "the fixture must contain the payload line this edits"
    target_before = host.slot(0, ref.location["fx_index"]).block
    moved = host.read_state_chunk(ref)
    assert moved != chunk, "the fixture must actually move the chunk"
    stale = TargetedStateSlice(
        chunk_digest=text_digest(chunk),  # the CHUNK digest the plan was built against
        line_start=_slice.line_start,
        line_end=_slice.line_end,
        header_line=_slice.header_line,
        class_hex=_slice.class_hex,
        ordinal=_slice.ordinal,
        block_text=_slice.block_text,
    )
    new_block = M.TargetedStateSlice(
        chunk_digest=stale.chunk_digest,
        line_start=stale.line_start,
        line_end=stale.line_end,
        header_line=stale.header_line,
        class_hex=stale.class_hex,
        ordinal=stale.ordinal,
        block_text=stale.block_text,
    )
    host.write_state_slice(ref, stale, new_block, timing=M.TimingProfile())
    assert host.slot(0, ref.location["fx_index"]).block == target_before, (
        "the re-located write must land on the target's span"
    )
    assert "Pk9uUFFz" in host.slot(0, 0).block, (
        "the sibling's change must survive: only the target's span is spliced"
    )


def test_p5_4c_a_slice_whose_block_did_change_is_refused():
    """The refusal that MUST stay: the plugin's own state moved since the plan.

    A re-location is only safe while the block holds what the plan was built from; a block whose
    bytes differ means someone else changed the plugin, and splicing our planned bytes over that
    change would silently destroy it.
    """
    host = build_host()
    transaction, host = make(request={"state": {"settings.hq": False}}, host=host, dry_run=False)
    _slice, ref, facts, chunk = slice_and_ref(host)
    # the plugin's OWN block changed: an external edit inside the addressed state
    target = host.slot(0, ref.location["fx_index"])
    target.block = target.block.replace("eyJtZXRhIjp7ImF1dGhvciI6ImRlbW8i", "eyJtZXRhIjp7ImF1dGhvciI6ImVkaXRlZCI")
    assert target.block != _slice.block_text, "the fixture must actually change the block"
    stale = TargetedStateSlice(
        chunk_digest=text_digest(chunk),
        line_start=_slice.line_start,
        line_end=_slice.line_end,
        header_line=_slice.header_line,
        class_hex=_slice.class_hex,
        ordinal=_slice.ordinal,
        block_text=_slice.block_text,
    )
    new_block = M.TargetedStateSlice(
        chunk_digest=stale.chunk_digest,
        line_start=stale.line_start,
        line_end=stale.line_end,
        header_line=stale.header_line,
        class_hex=stale.class_hex,
        ordinal=stale.ordinal,
        block_text=stale.block_text,
    )
    try:
        host.write_state_slice(ref, stale, new_block, timing=M.TimingProfile())
    except Exception as exc:  # noqa: BLE001 - typed refusal asserted by name
        assert type(exc).__name__ == "StateConflict"
        assert "block changed" in str(exc) or "block changed" in str(getattr(exc, "message", ""))
    else:  # pragma: no cover
        raise AssertionError("a slice whose BLOCK changed must be refused by the transport")


# ---------------------------------------------------------------------------
# Write-attempt accounting on the transport path (B-46)
# ---------------------------------------------------------------------------


def test_p5_write_attempt_accounting_is_exact_on_the_transport_path():
    host = build_host()
    transaction, host = make(
        request={"controls": {"tone_cutoff": 24.0, "tone_level": 0.4}},
        host=host,
        dry_run=False,
    )
    result = transaction.run()
    assert result.ok, result.error
    assert result.outcome.host.write_calls_attempted == 1, "one batched parameter write"
    assert result.outcome.host.write_effects_verified == 1
    assert result.outcome.host.write_effects_unknown == 0
    assert host.count("write_params") == 1
    idents = host.calls("write_params")[0][1]["idents"]
    assert len(idents) == 2, "both controls travelled in one hop"
    assert_counters_consistent(result)


def test_p5_a_parameter_that_never_lands_is_readback_mismatch_with_truthful_counters():
    host = build_host()
    host.inject("write_params", "store-nothing", times=1)
    transaction, host = make(
        request={"controls": {"tone_cutoff": 24.0}}, host=host, dry_run=False
    )
    result = transaction.run()
    assert not result.ok
    assert result.outcome.host.write_calls_attempted >= 1
    assert_counters_consistent(result)
    # the mismatch is reported in the channel evidence, not smoothed over
    parameters = result.outcome.channels["parameters"]
    assert parameters.mismatched or any(
        row.get("restore") for row in parameters.rows
    ), parameters.rows


# ---------------------------------------------------------------------------
# Regression tests for the independent audit findings (2026-10-10)
# ---------------------------------------------------------------------------


def test_p5_1e_a_line_break_that_only_str_splitlines_sees_cannot_shift_the_span():
    """A form feed (or NEL/LS/PS) is a line break for `str.splitlines` but not for
    `chunk.split("\n")`, which is the line space the locator numbers lines in.
    Splitting the splice with `splitlines` replaced the wrong lines and destroyed
    the block; every byte outside the span must survive here."""
    weird_line = 'NAME "Track\x0cweird\u2028name\x85end"'
    target_block = F.make_block_text(F.demo_document())
    after_block = F.foreign_block_text(
        name="VST3i: Third", module="third.vst3", class_hex="99998888777766665555444433332222"
    )
    body = "\n".join(["<TRACK", weird_line, target_block, after_block, ">"])
    slice_ = R.locate_block_span(
        body, class_hex=F.DEMO_CLASS, module=F.DEMO_MODULE, ordinal=0, matching_fx_count=1
    )
    assert slice_.line_start == 2

    new_text = F.make_block_text(F.demo_document(), width=80)
    spliced = R.splice_block_text(body, slice_, new_text)

    lines = body.split("\n")
    expected = (
        "\n".join(lines[: slice_.line_start])
        + "\n"
        + new_text
        + "\n"
        + "\n".join(lines[slice_.line_end + 1 :])
    )
    assert spliced == expected, "the splice must work in the locator's line space"
    assert weird_line in spliced, "the earlier line must survive verbatim"
    assert len(R.parse_blocks(spliced)) == 2, "both sibling blocks must still parse"


def test_p5_1f_a_span_at_the_end_of_the_chunk_gains_no_line_ending():
    target_block = F.make_block_text(F.demo_document())
    chunk = "<TRACK\n" + target_block  # the block's last line ends the chunk
    lines = chunk.split("\n")
    slice_ = M.TargetedStateSlice(
        chunk_digest=text_digest(chunk),
        line_start=1,
        line_end=len(lines) - 1,
        header_line=target_block.split("\n")[0],
        class_hex=F.DEMO_CLASS,
        ordinal=0,
        block_text=target_block,
    )
    assert lines[slice_.line_end] == target_block.split("\n")[-1], "the span ends the chunk"
    spliced = R.splice_block_text(chunk, slice_, "NEWBLOCK")
    assert spliced == "<TRACK\nNEWBLOCK"
    assert not spliced.endswith("\n"), "a span at EOF must not invent a line ending"


def test_p5_1g_the_read_back_relocation_matches_class_and_module():
    """A sibling block sharing the class id but not the module must not be
    mistaken for the written block (its digest is the write's evidence)."""
    class_hex = "56535456697461766974616C00000000"
    vst2 = f'<VST "Synth" Vital.vst2 0 "" {class_hex} 0 2 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0>'
    vst3 = f'<VST "Synth" Vital.vst3 0 "" {class_hex} 0 2 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0>'
    chunk = "\n".join(["<TRACK", vst2, "AAAA", ">", vst3, "BBBB", ">", ">"])
    bridge = R.ReaperHostBridge()
    located = bridge._locate_by_class(chunk, class_hex=class_hex, module="Vital.vst3", ordinal=0)
    assert "Vital.vst3" in located.header_line and "BBBB" in located.block_text
    located_vst2 = bridge._locate_by_class(chunk, class_hex=class_hex, module="Vital.vst2", ordinal=0)
    assert "AAAA" in located_vst2.block_text
    with pytest.raises(Exception) as excinfo:
        bridge._locate_by_class(chunk, class_hex=None, module=None, ordinal=0)
    assert type(excinfo.value).__name__ == "Unreadable"
