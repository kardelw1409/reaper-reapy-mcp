"""P40: two measured write-channel laws that decide whether a table ARRIVES, and one that decides
whether the caller is TOLD when it does not.

MEASURED (2026-10-18, live, then reproduced offline here):

1. **A changed TRACK is not a changed PLUGIN.** Starting the transport rewrote the track chunk
   while a transaction was walking its units, so a slice located a second earlier no longer matched
   its chunk. The transport refused the write - and the ROLLBACK hit the same guard
   (`channels.state = false`), which left a ballooned slot holding its stub. The rule is now: if the
   chunk moved, re-locate this instance's own block by identity and splice there; only a block whose
   BYTES differ is a conflict. Measured live: the same six-preset batch that failed 2 of 6 with the
   transport playing came out 6/6 once this landed.

2. **The order of the writes decides whether a balloon fits.** A balloon's projection is computed
   against the document; if the plan balloons first, that document still holds the OTHER slots' old
   tables. Delivering a slot that SHRINKS first makes the room. MEASURED offline: a live-shaped
   document (`slot 2` about to shrink by 60 KB, `slot 1` holding 1.4 MB) is refused for `slot 1` by
   the read ceiling before the reorder and delivered after it.

3. **A refusal must reach the caller.** The ceiling refusal existed in `outcome.named_gaps` while
   the response said `ALL_VERIFIED`, `partial: false`, `gaps: []`. Live consequence: two presets of
   a family batch left an oscillator holding the PREVIOUS table and nothing in the answer said so.

Mock host + fixtures only: no live REAPER.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PLUGINS = Path(__file__).resolve().parents[2] / "src" / "reaper_reapy_mcp" / "plugins"
if str(PLUGINS.parents[1]) not in sys.path:
    sys.path.insert(0, str(PLUGINS.parents[1]))

import mock_host as MH  # noqa: E402

from reaper_reapy_mcp.plugins import api as A  # noqa: E402
from reaper_reapy_mcp.plugins import model as M  # noqa: E402
from reaper_reapy_mcp.plugins.providers.vital import codec as VC  # noqa: E402

import test_p7_vital as T7  # noqa: E402
import test_p39_balloon as P39  # noqa: E402
import test_p39c_audio_slots as P39C  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

INSTANCE = {"track_index": 0, "fx_index": 0}
LIVE_VERSION = "1.5.5"
SAMPLE_CHARS = 217_108


def live_document_with_sample(*, shrinkable: bool) -> dict:
    """A document shaped like the measured live state after a big family preset.

    Measured 2026-10-18: after applying one family preset the live document was 1,660,148 chars -
    oscillator 2 holding a 1.4 MB audio table, the other two slots on Init, and a 217 KB sample
    block. The incoming preset replaces that audio table with a slightly smaller one (the write
    that can make room) and needs a full-size balloon for oscillator 1.
    """
    big = P39C.audio_table(samples=528_388, name="BIG LIVE")
    document = P39.live_document(P39.full_table(frames=1), big, P39.full_table(frames=1))
    document["synth_version"] = LIVE_VERSION
    document["settings"]["sample"] = {
        "name": "circuit_52",
        "length": 81_415,
        "samples": "A" * SAMPLE_CHARS,
    }
    return document


#: The targets of the arriving change set: a full-size table for oscillator 1 (needs a balloon), a
#: SMALLER audio table for oscillator 2 (a shrink, so it is scheduled first), a small table for
#: oscillator 3 (a small balloon).
def targets() -> dict:
    return {
        "big": P39C.audio_table(samples=528_388, name="BIG TARGET"),
        "shrink": P39C.audio_table(samples=430_000, name="SHRINK"),
        "small": P39C.audio_table(samples=32_768, name="SMALL"),
    }


def host_with(document: dict) -> MH.MockHostBridge:
    host = MH.MockHostBridge(
        tracks=[MH.MockTrack(0, "Track 1", [T7.vitals_slot(block=T7.build_block(document))])]
    )
    host.slot(0, 0).version = LIVE_VERSION
    return host


def decode_live(host: MH.MockHostBridge) -> dict:
    codec = T7.provider_on_fixtures().codec()
    ref = M.PluginInstanceRef(
        host="reaper", location=dict(INSTANCE), provider="vital", identity=T7.IDENTITY
    )
    return codec.decode(
        host.locate_state_slice(ref, host.fx_facts(0, 0), host.read_state_chunk(ref))
    )


# ---------------------------------------------------------------------------
# 1. The shrinking slot's write is scheduled first, and that is what makes room
# ---------------------------------------------------------------------------


def test_a_balloon_is_planned_against_the_document_it_will_meet():
    """The order of the writes decides whether a balloon fits under the read ceiling.

    MEASURED (offline reproduction of the live state): planning the balloon against the LIVE
    document refuses it (`document + room` crosses the ceiling), while planning it against the
    document the schedule leaves behind - after the shrinking write, which lands because a shrink
    always does - fits. The second number is the one the instance actually meets.
    """
    document = live_document_with_sample(shrinkable=True)
    want = targets()
    host = host_with(document)
    codec = T7.provider_on_fixtures().codec()
    live = decode_live(host)

    plan = codec.plan_state_writes(
        live,
        {
            "paths": {
                "settings.wavetables.0": want["big"],
                "settings.wavetables.1": want["shrink"],
                "settings.wavetables.2": want["small"],
            }
        },
    )
    assert [notice["reason"] for notice in plan.notices] == [], plan.notices
    balloons = {
        str(unit.payload["balloon"]["path"]): unit.payload["balloon"]
        for unit in plan.units
        if unit.payload.get("balloon")
    }
    assert "settings.wavetables.0" in balloons, sorted(balloons)
    balloon = balloons["settings.wavetables.0"]
    live_chars = VC._document_chars(live["document"])
    replaced = VC.payload_size(live["document"]["settings"]["wavetables"][0])
    old_projection = live_chars - replaced + balloon["room_chars"]
    assert old_projection > VC._state_ceiling_chars(), (
        "the fixture must actually be refused by the OLD order, or it proves nothing",
        old_projection,
        VC._state_ceiling_chars(),
    )
    shrink_delta = (
        VC.payload_size(want["shrink"])
        - VC.payload_size(live["document"]["settings"]["wavetables"][1])
    )
    assert shrink_delta < 0, "the fixture's second slot must SHRINK"
    scheduled = live_chars + shrink_delta - replaced + balloon["room_chars"]
    assert scheduled <= VC._state_ceiling_chars(), (
        "with the shrinking write scheduled first the same balloon fits",
        scheduled,
        VC._state_ceiling_chars(),
    )


def test_the_shrinking_table_write_is_scheduled_before_the_balloon():
    document = live_document_with_sample(shrinkable=True)
    want = targets()
    host = host_with(document)
    codec = T7.provider_on_fixtures().codec()
    plan = codec.plan_state_writes(
        decode_live(host),
        {
            "paths": {
                "settings.wavetables.0": want["big"],
                "settings.wavetables.1": want["shrink"],
                "settings.wavetables.2": want["small"],
            }
        },
    )
    order: list[str] = []
    for unit in plan.units:
        payload = unit.payload or {}
        if payload.get("balloon"):
            order.append("balloon:" + str(payload["balloon"]["path"]))
        elif payload.get("wavetable"):
            order.append("table:" + str(payload["wavetable"][0]["path"]))
    assert order[0] == "table:settings.wavetables.1", (
        "the write that SHRINKS the document must go first: it is what makes room",
        order,
    )
    assert order.index("balloon:settings.wavetables.0") > 0, order


def test_a_fresh_instance_fits_all_three_tables_however_big():
    """MEASURED (2026-10-18, live, twice): the family's biggest preset - three `Audio File Source`
    tables totalling 2.9 MB - applies fully onto a FRESH instance, and the ordering is what makes
    it fit: among writes that each need a balloon, the BIGGEST room must meet the emptiest document
    (ordering the smallest first instead cost the third table: 2/3 slots, measured).

    The peaks below are computed the way the codec computes them - against the RUNNING document -
    because that number is the plan's own claim about readability.
    """
    init = P39.full_table(frames=1)  # the measured Init-like table: one FULL frame, ~11 KB
    document = P39.live_document(init, init, init)
    document["synth_version"] = LIVE_VERSION
    host = host_with(document)
    codec = T7.provider_on_fixtures().codec()
    want = targets()
    live = decode_live(host)
    plan = codec.plan_state_writes(
        live,
        {
            "paths": {
                "settings.wavetables.0": want["big"],
                "settings.wavetables.1": want["big"],
                "settings.wavetables.2": want["small"],
            }
        },
    )
    assert [notice["reason"] for notice in plan.notices] == [], plan.notices
    balloons = [
        (str(unit.payload["balloon"]["path"]), int(unit.payload["balloon"]["room_chars"]))
        for unit in plan.units
        if unit.payload.get("balloon")
    ]
    assert len(balloons) == 3, balloons
    assert balloons[0][1] >= balloons[-1][1], (
        "the biggest room is opened first, against the emptiest document",
        balloons,
    )
    live_chars = VC._document_chars(live["document"])
    running = live_chars
    for unit in plan.units:
        payload = unit.payload or {}
        for path in list((payload.get("paths") or {}).keys()):
            index = int(str(path).split(".")[-1])
            if payload.get("balloon"):
                peak = running - VC.payload_size(init) + int(payload["balloon"]["room_chars"])
                assert peak <= VC._state_ceiling_chars(), (path, peak, VC._state_ceiling_chars())
            running += VC.payload_size(want["big"] if index < 2 else want["small"]) - VC.payload_size(init)
    assert running <= VC._state_ceiling_chars(), "the final state must also stay readable"


# ---------------------------------------------------------------------------
# 2. A refusal reaches the caller's envelope
# ---------------------------------------------------------------------------


@pytest.fixture
def vital_api(tmp_path, monkeypatch):
    from reaper_reapy_mcp.plugins.providers import registry as PR

    monkeypatch.setenv("REAPER_MCP_PLUGIN_PROVIDERS", "vital")
    monkeypatch.setenv("REAPER_MCP_PLUGIN_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(A, "_REGISTRY", None)
    monkeypatch.setitem(PR._FACTORIES, "vital", lambda: T7.provider_on_fixtures())
    return {"config": A.PluginConfig(provider_ids=("vital",), cache_dir=str(tmp_path / "cache"), env={})}


def test_the_tool_is_partial_and_names_a_table_the_ceiling_refused(vital_api, monkeypatch):
    """MEASURED before the fix: `ok: true`, `partial: false`, `gaps: []` - and the slot kept its
    old table. The verdict is about the units that RAN; these gaps are about what did not arrive.
    """
    big = P39C.audio_table(samples=528_388, name="BIG TARGET")
    document = live_document_with_sample(shrinkable=True)
    host = host_with(document)
    monkeypatch.setattr(A, "_BRIDGE", host)

    envelope = A.plugin_write(
        config=vital_api["config"],
        instance=dict(INSTANCE),
        request={"state": {"settings.wavetables.0": big}},
        dry_run=False,
    )
    data = envelope["data"]
    assert envelope["ok"] is True
    assert data["partial"] is True, "a change that did not arrive must mark the response partial"
    reasons = {gap["reason"] for gap in data["gaps"]}
    assert "state_ceiling_reached" in reasons, data["gaps"]
    assert data["write_gaps"]["from_outcome"] >= 1
    ceiling_gap = next(gap for gap in data["gaps"] if gap["reason"] == "state_ceiling_reached")
    assert ceiling_gap["path"] == "settings.wavetables.0"
    assert "4 MB" in str(ceiling_gap["message"])


def test_a_write_with_no_refused_change_stays_complete(vital_api, monkeypatch):
    """The flag has to be able to stay false, or it is not information."""
    target = P39C.audio_table(samples=2048, name="SMALL")
    document = live_document_with_sample(shrinkable=True)
    host = host_with(document)
    monkeypatch.setattr(A, "_BRIDGE", host)

    envelope = A.plugin_write(
        config=vital_api["config"],
        instance=dict(INSTANCE),
        request={"state": {"settings.wavetables.2": target}},
        dry_run=False,
    )
    data = envelope["data"]
    assert envelope["ok"] is True, envelope.get("error")
    assert data.get("gaps") in (None, []), data.get("gaps")
    assert data.get("partial") is not True
