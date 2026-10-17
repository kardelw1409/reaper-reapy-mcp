"""P38 - a routing the PLUGIN drops is a named gap, not a failed transaction (2026-10-17).

Measured live on Vital 1.5.5 (`probe_route_name_vs_growth.py`, run
`20261017T180410Z__vital-preset-research__39280`): a modulation pair whose
destination the running build no longer carries is **accepted by the host** and
then silently re-serialised out of the document - the slot reads back empty,
while the same write shape with a name the build does know lands byte-exact
(block delta +44). The plugin's own preset load drops such a pair in exactly the
same way, so the layer mirrors it instead of rolling the whole apply back: the
unit is verified (the drop IS the finding), the pair is named as a gap, and the
transaction continues with `partial: true`.

What must NOT change: every other non-landing unit still refuses and rolls back -
a pair that half-landed, a pair that landed with the wrong depth, an unresolved
slot, a plain path write. The shape test for "the plugin dropped it" is
deliberately narrow, and these tests pin both sides of it.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
PLUGINS_SRC = Path(__file__).resolve().parents[2] / "src"
if str(PLUGINS_SRC) not in sys.path:
    sys.path.insert(0, str(PLUGINS_SRC))

import mock_host as MH  # noqa: E402
import fixtures as F  # noqa: E402
from mock_provider import DemoCodec, MockProvider  # noqa: E402

from reaper_reapy_mcp.plugins import api as A  # noqa: E402
from reaper_reapy_mcp.plugins import tools as T  # noqa: E402
from reaper_reapy_mcp.plugins.model import StateWriteUnit  # noqa: E402
from reaper_reapy_mcp.plugins.providers import registry as PR  # noqa: E402
from reaper_reapy_mcp.plugins.transaction import _route_dropped_by_plugin  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


class DroppingCodec(DemoCodec):
    """Codec A plus the measured plugin behaviour for an unknown pair name.

    Two things differ from codec A, and both mirror the real engine as measured:
    a create is SLOT-ADDRESSED (the engine keeps the slot it wrote into, which is
    what makes the read-back evidence readable at all), and a pair whose
    destination the build does not know is re-serialised OUT of it - the slot
    stays, holding an empty pair. Anything else is written as asked, which is what
    makes the negative cases (a pair that landed, a depth that did not)
    meaningful.
    """

    def __init__(self, unknown: list[str] | None = None, *, zero_amount: list[str] | None = None) -> None:
        self.unknown = set(unknown or [])
        self.zero_amount = set(zero_amount or [])
        self._slot_counter = 1

    def _assign_slot(self) -> int:
        slot = self._slot_counter
        self._slot_counter += 1
        return slot

    def encode_slice(self, doc, unit, current):  # type: ignore[override]
        payload = dict(unit.payload or {})
        op = payload.get("route_op")
        if isinstance(op, dict) and op.get("op") == "create":
            resolved = dict(op)
            if resolved.get("slot") is None:
                resolved["slot"] = self._assign_slot()
            if resolved.get("destination") in self.unknown:
                resolved.update({"source": "", "destination": "", "amount": 0.0})
            elif resolved.get("destination") in self.zero_amount:
                resolved.update({"amount": 0.0})
            if resolved != op:
                unit = StateWriteUnit(
                    order_index=unit.order_index,
                    kind=unit.kind,
                    payload={**payload, "route_op": resolved},
                )
        return super().encode_slice(doc, unit, current)

    def _apply_route_op(self, doc, op):  # type: ignore[override]
        """A named create takes ITS slot (the real engine's own rule)."""
        slot = op.get("slot")
        if op.get("op") == "create" and slot is not None:
            routes = doc.setdefault("routes", [])
            row = next((entry for entry in routes if entry.get("slot") == slot), None)
            if row is None:
                row = {"slot": int(slot), "source": "", "destination": "", "amount": 0.0, "bipolar": False}
                routes.append(row)
            row.update(
                {
                    "source": op.get("source") or "",
                    "destination": op.get("destination") or "",
                    "amount": float(op.get("amount") or 0.0),
                }
            )
            return
        return super()._apply_route_op(doc, op)


class SlotKeepingMockProvider(MockProvider):
    """The demo provider plus the slot pass-through the P38 change set relies on.

    The real provider passes the preset file's own route slot through to the
    route operation, and the unit is then keyed by it - which is what makes the
    read-back evidence readable ("slot 4 came back with an empty pair") instead of
    "the pair was not found".
    """

    def normalize_request(self, profile, request):  # type: ignore[override]
        resolved = super().normalize_request(profile, request)
        by_pair = {
            (spec.get("source"), spec.get("destination")): spec.get("slot")
            for spec in (request.get("routes_create") or [])
        }
        for row in resolved.routes_create:
            slot = by_pair.get((row.get("source"), row.get("destination")))
            if slot is not None:
                row["slot"] = slot
        return resolved


def empty_route_table(host: MH.MockHostBridge) -> None:
    """Start from a document with no routings, so every pair is the test's own.

    The addressed slot is the demo INSTANCE (fx_index 1; index 0 is the foreign
    slot the engine must not touch).
    """
    document = F.demo_document()
    document["routes"] = []
    host.external_set_document(document, track_index=0, fx_index=1)


def build_host() -> MH.MockHostBridge:
    foreign = MH.MockFxSlot(
        name=F.FOREIGN_NAME,
        module=F.FOREIGN_MODULE,
        class_id=F.FOREIGN_CLASS,
        block=F.foreign_block_text(),
        params={"0:9000": 0.1},
    )
    return MH.MockHostBridge(tracks=[MH.MockTrack(0, "Track 1", [foreign, MH.MockFxSlot()])])


@pytest.fixture
def demo(tmp_path, monkeypatch):
    """The P9 demo server (mock host + demo provider), with the codec replaceable."""
    from mcp.server.fastmcp import FastMCP

    host = build_host()
    monkeypatch.setenv("REAPER_MCP_PLUGIN_PROVIDERS", "demo")
    monkeypatch.setenv("REAPER_MCP_PLUGIN_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(A, "_BRIDGE", host)
    monkeypatch.setattr(A, "_REGISTRY", None)
    factory: dict = {"make": lambda: MockProvider()}
    monkeypatch.setitem(PR._FACTORIES, "demo", lambda: factory["make"]())
    config = A.PluginConfig(provider_ids=("demo",), cache_dir=str(tmp_path / "cache"), env={})
    mcp = FastMCP("probe")
    T.setup_plugin_tools(mcp, config=config)
    yield {"host": host, "mcp": mcp, "config": config, "factory": factory}


def call(mcp, name: str, arguments: dict | None = None) -> dict:
    blocks = asyncio.run(mcp.call_tool(name, arguments or {}))
    text = next(block.text for block in blocks if getattr(block, "text", None))
    return json.loads(text)


def routes_on_the_slot(host: MH.MockHostBridge) -> list[dict]:
    chunk = host.slot(0, 1).block
    document = F.decode_block_document(chunk)
    return [row for row in document.get("routes", []) if row.get("destination")]


# ---------------------------------------------------------------------------
# The measured drop shape: named, tolerated, and the transaction continues
# ---------------------------------------------------------------------------


def test_a_dropped_pair_is_named_and_the_transaction_continues(demo):
    empty_route_table(demo["host"])
    demo["factory"]["make"] = lambda: SlotKeepingMockProvider(codec=DroppingCodec(unknown=["tone_cutoff"]))
    envelope = call(
        demo["mcp"],
        "plugin_write",
        {
            "instance": {"track_index": 0, "fx_index": 1},
            "request": {
                "routes_create": [
                    {"slot": 1, "source": "src_a", "destination": "tone_cutoff", "amount": 0.5},
                    {"slot": 2, "source": "src_b", "destination": "tone_level", "amount": 0.25},
                ]
            },
            "dry_run": False,
        },
    )
    assert envelope["ok"] is True, envelope
    assert envelope["outcome"]["verdict"] == "ALL_VERIFIED"
    assert envelope["outcome"]["final_state"] == "committed"

    # the drop is a FINDING: named, with its read-back evidence, never implied to
    # have landed
    gaps = [gap for gap in envelope["outcome"]["named_gaps"] if gap["reason"] == "route_dropped_by_plugin"]
    assert len(gaps) == 1, envelope["outcome"]["named_gaps"]
    assert gaps[0]["route"]["destination"] == "tone_cutoff"
    assert gaps[0]["mismatched"], "the gap must carry what the read-back actually said"
    assert envelope["data"]["dropped_routes"][0]["destination"] == "tone_cutoff"
    assert envelope["data"]["partial"] is True

    # ...and the rest of the batch still landed (no rollback, the second unit ran)
    assert demo["host"].count("write_state_slice") == 2
    assert [row["destination"] for row in routes_on_the_slot(demo["host"])] == ["tone_level"]
    rollback = envelope["outcome"]["rollback"]
    assert not rollback.get("attempted"), "a tolerated drop must not trigger a rollback"


def test_a_pair_that_landed_with_the_wrong_depth_still_rolls_back(demo):
    """The narrowness that keeps the tolerance honest: a pair that LANDED is a
    real verification failure, whatever else disagrees.

    The fixture keeps the demo document's own route table (a rollback is expressed
    as captured document paths, so an empty list is not a restorable state there -
    which is the mock's declared shape, not a property of the engine).
    """
    demo["factory"]["make"] = lambda: SlotKeepingMockProvider(codec=DroppingCodec(zero_amount=["tone_cutoff"]))
    envelope = call(
        demo["mcp"],
        "plugin_write",
        {
            "instance": {"track_index": 0, "fx_index": 1},
            "request": {
                "routes_create": [
                    {"slot": 1, "source": "src_a", "destination": "tone_cutoff", "amount": 0.5}
                ]
            },
            "dry_run": False,
        },
    )
    assert envelope["ok"] is False, envelope
    assert envelope["error"]["code"] == "WriteNotApplied"
    assert envelope["outcome"]["verdict"] == "FAILED_AND_RESTORED"
    # `unchanged_verified` is the honest final state here: the rollback put the
    # captured document back, so the instance is verified to be as it was
    assert envelope["outcome"]["final_state"] == "unchanged_verified"
    kept = [(row["slot"], row["source"], row["destination"], row["amount"]) for row in routes_on_the_slot(demo["host"])]
    assert kept == [(1, "src_a", "tone_cutoff", 0.25), (2, "src_b", "tone_level", 0.0)], kept
    assert not [gap for gap in envelope["outcome"]["named_gaps"] if gap["reason"] == "route_dropped_by_plugin"]


# ---------------------------------------------------------------------------
# The shape test itself (synthetic rows: no host, no document)
# ---------------------------------------------------------------------------


class _Report:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.mismatched = [row for row in rows if not row["agrees"]]


def _pair_row(field: str, written, read_back) -> dict:
    return {
        "subject": {"route": 4},
        "field": field,
        "written": written,
        "read_back": read_back,
        "agrees": written == read_back,
        "channel": "state",
    }


def _unit(op: dict | None, kind: str = "state") -> StateWriteUnit:
    payload = {"route_op": op} if op is not None else {"paths": {"settings.mode": 1}}
    return StateWriteUnit(order_index=0, kind=kind, payload=payload)


def test_the_whole_empty_pair_is_the_drop_shape():
    rows = [
        _pair_row("source", "src_a", ""),
        _pair_row("destination", "tone_cutoff", ""),
        _pair_row("amount", 0.5, 0.0),
    ]
    assert _route_dropped_by_plugin(_unit({"op": "create", "source": "src_a", "destination": "tone_cutoff"}), _Report(rows))


def test_a_part_of_the_pair_that_landed_is_not_a_drop():
    rows = [
        _pair_row("source", "src_a", "src_a"),
        _pair_row("destination", "tone_cutoff", ""),
    ]
    assert not _route_dropped_by_plugin(
        _unit({"op": "create", "source": "src_a", "destination": "tone_cutoff"}), _Report(rows)
    )


def test_a_missing_slot_address_is_not_a_drop():
    rows = [{"subject": {"route": 4}, "written": "present", "read_back": None, "agrees": False, "channel": "state"}]
    assert not _route_dropped_by_plugin(
        _unit({"op": "create", "source": "src_a", "destination": "tone_cutoff"}), _Report(rows)
    )


def test_an_unresolved_slot_row_is_not_a_drop():
    rows = [{"subject": {"route": None}, "written": "create", "read_back": None, "agrees": False, "channel": "state"}]
    assert not _route_dropped_by_plugin(
        _unit({"op": "create", "source": "src_a", "destination": "tone_cutoff"}), _Report(rows)
    )


def test_a_path_row_keeps_the_unit_a_real_failure():
    rows = [
        _pair_row("source", "src_a", ""),
        {"path": "settings.mode", "written": 1, "read_back": 0, "agrees": False, "channel": "state"},
    ]
    assert not _route_dropped_by_plugin(_unit({"op": "create", "source": "src_a"}), _Report(rows))


def test_only_create_and_update_routes_are_tolerated():
    rows = [_pair_row("source", "src_a", ""), _pair_row("destination", "tone_cutoff", "")]
    assert not _route_dropped_by_plugin(_unit({"op": "remove", "slot": 4}), _Report(rows))
    assert not _route_dropped_by_plugin(_unit(None), _Report(rows))


def test_a_report_that_agreed_has_nothing_to_tolerate():
    rows = [_pair_row("source", "src_a", "src_a"), _pair_row("destination", "tone_cutoff", "tone_cutoff")]
    assert not _route_dropped_by_plugin(
        _unit({"op": "create", "source": "src_a", "destination": "tone_cutoff"}), _Report(rows)
    )
