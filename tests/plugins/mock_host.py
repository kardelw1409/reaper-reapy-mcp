"""MockHostBridge - in-memory host double with a call log and failure injection.

Implements the full `HostBridge` contract (reads and writes). Nothing here
pretends the host is simple: the mock exposes every failure mode it fakes
(`raise`, `wedge`, `store-nothing`, `drop-after-return`), records a call log
(order + counts) for batching/order assertions, keeps byte-accurate multi-block
chunks, and offers a replaceable instance handle plus external-mutation hooks so
the external-change cases run offline (12 §6).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Callable

import fixtures as F

from reaper_reapy_mcp.plugins.errors import (
    InstanceNotFound,
    NotConnected,
    StateConflict,
    TargetSliceAmbiguous,
    Unreadable,
    Wedged,
)
from reaper_reapy_mcp.plugins.model import (
    FxHostFacts,
    PluginInstanceRef,
    StateWriteUnit,
    TargetedStateSlice,
    TimingProfile,
    TrackView,
    WriteAttempt,
    canonical_json,
    text_digest,
    utc_now_iso,
)

#: Injection modes the mock can produce on demand.
INJECTION_MODES = (
    "noop",
    "raise",
    "wedge",
    "store-nothing",
    "store-wrong",
    "drop-after-return",
)


class MockFxSlot:
    """One FX slot: identity facts, a parameter store keyed by ident, a block."""

    def __init__(
        self,
        *,
        name: str = F.DEMO_NAME,
        module: str = F.DEMO_MODULE,
        class_id: str = F.DEMO_CLASS,
        block: str = "",
        params: dict[str, float] | None = None,
        version: str = "1.0.0",
        param_names: dict[int, str] | None = None,
        session_handle: str | None = None,
    ) -> None:
        self.name = name
        self.module = module
        self.class_id = class_id
        self.version = version
        self.session_handle = session_handle
        self.params: dict[str, float] = dict(params if params is not None else F.demo_params())
        if param_names is not None:
            self.param_names: dict[int, str] = dict(param_names)
        else:
            self.param_names = {
                index: F.DEMO_PARAM_NAMES[index]
                for index in range(len(self.params))
                if index < len(F.DEMO_PARAM_NAMES)
            }
        self.missing_idents: set[str] = set()
        self.block = block or F.make_block_text(F.demo_document(), name=name, module=module, class_hex=class_id)
        #: P26 metadata channels. A slot that answers none of them models a plugin
        #: with no step sizes, no defaults and no section names - which is the
        #: common case and must be distinguishable from "nobody asked".
        self.step_sizes: dict[int, dict[str, Any]] = {}
        self.named_config: dict[int, dict[str, Any]] = {}
        self.sections: dict[int, str] | None = None
        self.display_fn: Callable[[int, float], str] | None = None
        self.display_calls = 0

    def display(self, index: int, normalized: float) -> str:
        """The slot's own display text for a value the *caller* passes.

        The default mirrors a plugin that prints the raw normalized value with
        three decimals, so a test's expectations do not depend on a fixture's
        formatting choices.
        """
        self.display_calls += 1
        if self.display_fn is not None:
            return str(self.display_fn(int(index), float(normalized)))
        return f"{float(normalized):.3f}"

    def step_sizes_for(self, index: int) -> dict[str, Any]:
        entry = dict(self.step_sizes.get(int(index)) or {})
        reported = bool(entry.get("reported"))
        return {
            "status": "ok" if reported or entry.get("is_toggle") else "absent",
            "reported": reported,
            "step": float(entry.get("step") or 0.0),
            "small": float(entry.get("small") or 0.0),
            "large": float(entry.get("large") or 0.0),
            "is_toggle": bool(entry.get("is_toggle")),
            "note": None if reported or entry.get("is_toggle") else "the host reports no step for this parameter",
        }

    @property
    def ident_by_index(self) -> dict[int, str]:
        return {index: ident for index, ident in enumerate(self.params)}

    @property
    def index_by_ident(self) -> dict[str, int]:
        return {ident: index for index, ident in enumerate(self.params)}

    def rename_parameter(self, index: int, name: str) -> None:
        """Host-side rename: the fixture for a build that reordered its parameters."""
        self.param_names[int(index)] = str(name)

    def facts(self, *, track_index: int, fx_index: int, track_name: str, fx_count: int) -> FxHostFacts:
        return FxHostFacts(
            track_index=track_index,
            fx_index=fx_index,
            fx_name=self.name,
            class_id=self.class_id,
            module=self.module,
            param_count=len(self.params),
            version=self.version,
            track_name=track_name,
            fx_count=fx_count,
            session_handle=self.session_handle,
        )

    def replace_identically(self, *, name: str | None = None, class_id: str | None = None,
                            block: str | None = None, version: str | None = None,
                            session_handle: str | None = None) -> None:
        """Identical-build replacement fixture: same slot, possibly new build.

        ``session_handle`` models the host reporting *a handle* - the live L-3
        measurement found REAPER reuses the same handle for a repopulated slot,
        so a fixture that changes it exercises the comparison, not the L-3 case.
        """
        if name is not None:
            self.name = name
        if class_id is not None:
            self.class_id = class_id
        if version is not None:
            self.version = version
        if session_handle is not None:
            self.session_handle = session_handle
        if block is not None:
            self.block = block


class MockTrack:
    def __init__(self, index: int, name: str, slots: list[MockFxSlot]) -> None:
        self.index = index
        self.name = name
        self.slots = slots

    def chunk(self) -> str:
        return F.track_chunk(slot.block for slot in self.slots)

    def set_chunk(self, chunk: str) -> None:
        """Re-derive slot blocks from a written chunk (blocks are in slot order)."""
        parsed = parse_blocks(chunk)
        if len(parsed) != len(self.slots):
            raise StateConflict(
                "mock track chunk/block count mismatch after a write",
                detail={"blocks": len(parsed), "slots": len(self.slots)},
            )
        for slot, block in zip(self.slots, parsed):
            slot.block = block["block_text"]


class MockHostBridge:
    """The full `HostBridge` contract over an in-memory world."""

    def __init__(
        self,
        tracks: list[MockTrack] | None = None,
        *,
        sleeper: Callable[[float], None] | None = None,
        reset_params_on_state_write: dict[str, float] | None = None,
    ) -> None:
        self.tracks = list(tracks) if tracks else [self.default_track()]
        self.log: list[tuple[str, dict[str, Any]]] = []
        self.counters: Counter[str] = Counter()
        self.sleeps: list[float] = []
        self.sleeper = sleeper or self.sleeps.append
        self.reset_params_on_state_write = dict(reset_params_on_state_write or {})
        self._injections: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._after_hooks: dict[tuple[str, int], Callable[[], None]] = {}

    @staticmethod
    def default_track() -> MockTrack:
        return MockTrack(0, "Track 1", [MockFxSlot()])

    # -- test-side helpers --------------------------------------------------

    def track(self, index: int = 0) -> MockTrack:
        for track in self.tracks:
            if track.index == index:
                return track
        raise InstanceNotFound(f"no mock track {index}")

    def slot(self, track_index: int = 0, fx_index: int = 0) -> MockFxSlot:
        track = self.track(track_index)
        if fx_index < 0 or fx_index >= len(track.slots):
            raise InstanceNotFound(
                f"no mock fx slot {fx_index} on track {track_index}",
                detail={"n_fx": len(track.slots)},
            )
        return track.slots[fx_index]

    def inject(self, label: str, mode: str, *, times: int = 1, error: Exception | None = None) -> None:
        if mode not in INJECTION_MODES:
            raise ValueError(f"unknown injection mode {mode!r}")
        for _ in range(times):
            self._injections[label].append({"mode": mode, "error": error})

    def inject_at(self, label: str, occurrence: int, mode: str, *, error: Exception | None = None) -> None:
        """Schedule ``mode`` for the ``occurrence``-th call of ``label`` only."""
        if occurrence < 1:
            raise ValueError("occurrence is 1-based")
        self.inject(label, "noop", times=occurrence - 1)
        self.inject(label, mode, error=error)

    def calls(self, label: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        if label is None:
            return list(self.log)
        return [row for row in self.log if row[0] == label]

    def count(self, label: str) -> int:
        return sum(1 for row in self.log if row[0] == label)

    def reset_log(self) -> None:
        self.log.clear()

    def after(self, label: str, occurrence: int, callback: Callable[[], None]) -> None:
        """Run ``callback`` once, at the start of the ``occurrence``-th call of
        ``label`` (the fixture for an external writer racing the transaction)."""
        self._after_hooks[(label, occurrence)] = callback

    # -- external (third-party) mutation hooks ------------------------------

    def external_set_param(self, ident: str, value: float, *, track_index: int = 0, fx_index: int = 0) -> None:
        slot = self.slot(track_index, fx_index)
        if ident not in slot.params:
            raise KeyError(ident)
        slot.params[ident] = float(value)

    def external_set_document(self, doc: dict[str, Any], *, track_index: int = 0, fx_index: int = 0) -> None:
        slot = self.slot(track_index, fx_index)
        header = slot.block.split("\n", 1)[0]
        slot.block = F.make_block_text(doc, header=header)

    def external_replace_slot(self, *, track_index: int = 0, fx_index: int = 0, **kwargs: Any) -> None:
        self.slot(track_index, fx_index).replace_identically(**kwargs)

    def external_swap_slot_object(
        self, *, track_index: int = 0, fx_index: int = 0, **kwargs: Any
    ) -> MockFxSlot:
        """A **new** slot object in the same position, indistinguishable by facts.

        `external_replace_slot` mutates the object the bridge already handed out;
        this one discards it and installs a fresh instance carrying the same
        exposed facts, the same block and the same parameter store contents. That
        is what the measured L-3 case is - a replacement, not a mutation - and the
        difference matters: a fixture that mutates in place cannot show that a
        caller's earlier reference is now pointing at an instance that no longer
        exists.
        """
        old = self.slot(track_index, fx_index)
        replacement = MockFxSlot(
            name=kwargs.get("name") or old.name,
            module=old.module,
            class_id=kwargs.get("class_id") or old.class_id,
            block=kwargs.get("block") or old.block,
            params=dict(old.params),
            version=kwargs.get("version") or old.version,
            param_names=dict(old.param_names),
            session_handle=kwargs.get("session_handle")
            if "session_handle" in kwargs
            else old.session_handle,
        )
        self.track(track_index).slots[fx_index] = replacement
        return replacement

    def external_move_slot(self, from_index: int, to_index: int, *, track_index: int = 0) -> None:
        track = self.track(track_index)
        slot = track.slots.pop(from_index)
        track.slots.insert(to_index, slot)

    # -- injection gate -----------------------------------------------------

    def _next_mode(self, label: str) -> str | None:
        pending = self._injections.get(label)
        if not pending:
            return None
        entry = pending.pop(0)
        mode = entry["mode"]
        if mode == "noop":
            return None
        if mode == "raise":
            raise entry["error"] or Wedged(f"injected failure on {label}", detail={"label": label})
        if mode == "wedge":
            raise Wedged(f"{label} did not return within its timeout", detail={"label": label})
        return mode

    def _record(self, label: str, **detail: Any) -> None:
        self.counters[label] += 1
        self.log.append((label, detail))
        hook = self._after_hooks.pop((label, self.counters[label]), None)
        if hook is not None:
            hook()

    # -- HostBridge: reads --------------------------------------------------

    def list_tracks(self) -> list[TrackView]:
        self._next_mode("list_tracks")
        self._record("list_tracks")
        return [TrackView(track.index, track.name, len(track.slots)) for track in self.tracks]

    def fx_facts(self, track_index: int, fx_index: int) -> FxHostFacts:
        mode = self._next_mode("fx_facts")
        track = self.track(track_index)
        slot = self.slot(track_index, fx_index)
        self._record("fx_facts", track=track_index, fx=fx_index)
        if mode == "store-nothing":
            raise Unreadable("injected: fx_facts produced nothing", detail={"label": "fx_facts"})
        return slot.facts(
            track_index=track_index,
            fx_index=fx_index,
            track_name=track.name,
            fx_count=len(track.slots),
        )

    def param_idents(self, ref: PluginInstanceRef, indices: list[int]) -> list[str | None]:
        self._next_mode("param_idents")
        slot = self._slot_for(ref)
        self._record("param_idents", n=len(indices))
        table = slot.ident_by_index
        return [
            None if table.get(index) in slot.missing_idents else table.get(index)
            for index in indices
        ]

    def param_index_from_idents(self, ref: PluginInstanceRef, idents: list[str]) -> list[int | None]:
        """P36: resolution at use time, mirroring the real verb's direct rule."""
        mode = self._next_mode("param_index_from_idents")
        slot = self._slot_for(ref)
        self._record("param_index_from_idents", n=len(idents))
        if mode == "store-nothing":
            return []
        table = slot.index_by_ident
        return [
            None if str(ident) in slot.missing_idents else table.get(str(ident))
            for ident in idents
        ]

    def param_names(self, ref: PluginInstanceRef, indices: list[int]) -> list[str | None]:
        self._next_mode("param_names")
        slot = self._slot_for(ref)
        self._record("param_names", n=len(indices))
        return [slot.param_names.get(index) for index in indices]

    def read_params(self, ref: PluginInstanceRef, indices: list[int]) -> list[float]:
        mode = self._next_mode("read_params")
        slot = self._slot_for(ref)
        self._record("read_params", n=len(indices))
        if mode == "store-nothing":
            return []
        table = slot.ident_by_index
        values: list[float] = []
        for index in indices:
            ident = table.get(index)
            if ident is None:
                values.append(0.0)
            else:
                values.append(float(slot.params[ident]))
        return values

    # -- P26 read-only metadata channels ------------------------------------

    def param_metadata(
        self,
        ref: PluginInstanceRef,
        indices: list[int],
        *,
        include: tuple[str, ...] = ("name", "ident", "value", "steps", "formatted", "named"),
    ) -> dict[str, Any]:
        """Batched metadata: one log entry for the whole list (like the real verb)."""
        mode = self._next_mode("param_metadata")
        slot = self._slot_for(ref)
        self._record("param_metadata", n=len(indices), channels=list(include))
        if mode == "store-nothing":
            return {"rows": [], "channels": {name: "absent" for name in include}, "calls": 0}
        wanted = set(str(name) for name in include)
        nametable = slot.index_by_ident
        idents = {index: ident for ident, index in nametable.items()}
        rows: list[dict[str, Any]] = []
        for index in indices:
            row: dict[str, Any] = {"index": int(index)}
            if "name" in wanted:
                row["name"] = slot.param_names.get(int(index))
            if "ident" in wanted:
                row["ident"] = None if slot.missing_idents else idents.get(int(index))
                if slot.missing_idents and idents.get(int(index)) in slot.missing_idents:
                    row["ident"] = None
            if "value" in wanted:
                ident = idents.get(int(index))
                row["value"] = float(slot.params[ident]) if ident is not None else None
            if "steps" in wanted:
                row["steps"] = slot.step_sizes_for(int(index))
            if "formatted" in wanted:
                ident = idents.get(int(index))
                row["formatted"] = (
                    slot.display(int(index), float(slot.params[ident])) if ident is not None else None
                )
            if "named" in wanted:
                entries = dict(slot.named_config.get(int(index)) or {})
                row["named_config"] = {
                    "status": "ok" if entries else "absent",
                    "entries": entries,
                    "probed": sorted(entries),
                }
            if slot.sections is not None and "section" in wanted:
                row["section"] = slot.sections.get(int(index))
            rows.append(row)
        channels = {name: "ok" for name in include}
        return {"rows": rows, "channels": channels, "calls": len(rows)}

    def param_sections(self, ref: PluginInstanceRef, indices: list[int]) -> dict[str, Any]:
        mode = self._next_mode("param_sections")
        slot = self._slot_for(ref)
        self._record("param_sections", n=len(indices))
        if mode == "store-nothing" or slot.sections is None:
            return {
                "status": "unsupported",
                "rows": [],
                "source": None,
                "reason": "the mock slot declares no parameter sections",
            }
        return {
            "status": "ok",
            "rows": [{"index": int(index), "section": slot.sections.get(int(index))} for index in indices],
            "source": "TrackFX_GetParamSectionName",
        }

    def format_params_normalized(
        self, ref: PluginInstanceRef, pairs: list[tuple[int, float]]
    ) -> dict[str, Any]:
        mode = self._next_mode("format_params_normalized")
        slot = self._slot_for(ref)
        self._record("format_params_normalized", n=len(pairs))
        if mode == "store-nothing":
            return {"texts": [], "calls": 0, "call": "TrackFX_FormatParamValueNormalized"}
        return {
            "texts": [slot.display(int(index), float(value)) for index, value in pairs],
            "calls": len(pairs),
            "call": "TrackFX_FormatParamValueNormalized",
        }

    def read_state_chunk(self, ref: PluginInstanceRef) -> str:
        mode = self._next_mode("read_state_chunk")
        track = self.track(int(ref.location.get("track_index", 0)))
        self._record("read_state_chunk")
        if mode == "store-nothing":
            return ""
        return track.chunk()

    def locate_state_slice(
        self,
        ref: PluginInstanceRef,
        facts: FxHostFacts,
        chunk: str,
        *,
        ordinal: int | None = None,
        matching_fx_count: int | None = None,
    ) -> TargetedStateSlice:
        self._record("locate_state_slice")
        if ordinal is None and matching_fx_count is None:
            ordinal, matching_fx_count = self._ordinal_for(ref, facts)
        return locate_block_span(
            chunk,
            class_hex=facts.class_id,
            module=facts.module,
            ordinal=ordinal,
            matching_fx_count=matching_fx_count,
        )

    # -- HostBridge: writes -------------------------------------------------

    def write_params(self, ref: PluginInstanceRef, pairs: list[tuple[str, float]]) -> WriteAttempt:
        mode = self._next_mode("write_params")
        slot = self._slot_for(ref)
        self._record("write_params", idents=list(dict(pairs).keys()))
        if mode == "store-wrong":
            for ident, value in pairs:
                if ident not in slot.params:
                    raise Unreadable(f"unknown parameter ident {ident!r}", detail={"ident": ident})
                slot.params[ident] = float(value) + 0.5
        elif mode != "store-nothing":
            for ident, value in pairs:
                if ident not in slot.params:
                    raise Unreadable(f"unknown parameter ident {ident!r}", detail={"ident": ident})
                slot.params[ident] = float(value)
        attempt = WriteAttempt(
            unit_index=-1,
            attempted_utc=utc_now_iso(),
            returned=True,
            slice_digest_sent=text_digest(canonical_json({"parameters": dict(pairs)})),
            readback_slice_digest=None,
            effects_verified=None,
        )
        if mode == "drop-after-return":
            raise NotConnected(
                "transport died after the write returned; the effect is unknown",
                detail={"label": "write_params"},
            )
        return attempt

    def write_state_slice(
        self,
        ref: PluginInstanceRef,
        slice: TargetedStateSlice,
        new_block: TargetedStateSlice,
        *,
        timing: TimingProfile,
    ) -> WriteAttempt:
        mode = self._next_mode("write_state_slice")
        track = self.track(int(ref.location.get("track_index", 0)))
        chunk = track.chunk()
        self._record("write_state_slice")
        if not slice.is_valid_for(chunk):
            raise StateConflict(
                "the slice no longer matches the live chunk; it must be re-located",
                detail={"slice_chunk_digest": slice.chunk_digest, "live_chunk_digest": text_digest(chunk)},
            )
        sent_digest = text_digest(new_block.block_text)
        if mode != "store-nothing":
            lines = chunk.split("\n")
            replacement = new_block.block_text.split("\n")
            spliced = "\n".join(lines[: slice.line_start] + replacement + lines[slice.line_end + 1 :])
            track.set_chunk(spliced)
            if self.reset_params_on_state_write:
                target = self.slot(
                    int(ref.location.get("track_index", 0)), int(ref.location.get("fx_index", 0))
                )
                for ident, value in self.reset_params_on_state_write.items():
                    if ident in target.params:
                        target.params[ident] = float(value)
        self.sleeper(float(timing.settle_s))
        attempt = WriteAttempt(
            unit_index=-1,
            attempted_utc=utc_now_iso(),
            returned=True,
            slice_digest_sent=sent_digest,
            readback_slice_digest=text_digest(track.chunk()),
            effects_verified=None,
        )
        if mode == "drop-after-return":
            raise NotConnected(
                "transport died after the state write returned; the effect is unknown",
                detail={"label": "write_state_slice"},
            )
        return attempt

    def re_read_slice(
        self, ref: PluginInstanceRef, facts: FxHostFacts, unit: StateWriteUnit
    ) -> TargetedStateSlice:
        self._next_mode("re_read_slice")
        chunk = self.read_state_chunk(ref)
        self._record("re_read_slice")
        return self.locate_state_slice(ref, facts, chunk)

    # -- HostBridge: transport discipline -----------------------------------

    def call(self, fn: Callable[..., Any], *args: Any, timeout: float, label: str) -> Any:
        self._next_mode(label)
        self._record("call", label=label, timeout=timeout)
        return fn(*args)

    def reconnect(self) -> None:
        self._record("reconnect")

    # -- internals ----------------------------------------------------------

    def _slot_for(self, ref: PluginInstanceRef) -> MockFxSlot:
        location = ref.location if isinstance(ref.location, dict) else {}
        return self.slot(int(location.get("track_index", 0)), int(location.get("fx_index", 0)))

    def _ordinal_for(self, ref: PluginInstanceRef, facts: FxHostFacts) -> tuple[int, int]:
        """Ordinal of the addressed slot among slots the host API can group by
        the name it reports for the target, plus that group's size."""
        track = self.track(int(ref.location.get("track_index", 0)))
        fx_index = int(ref.location.get("fx_index", 0))
        matches = [index for index, slot in enumerate(track.slots) if slot.name == facts.fx_name]
        if fx_index not in matches:
            return 0, len(matches)
        return matches.index(fx_index), len(matches)


# ---------------------------------------------------------------------------
# Chunk parsing / locating (mock-side; the production bridge implements the
# same rules in plugins/host/reaper.py and the P3 conformance suite compares
# both implementations on the same fixtures)
# ---------------------------------------------------------------------------

_CLASS_RE = re.compile(r"\b([0-9A-Fa-f]{32})\b")
_QUOTED_RE = re.compile(r'"([^"]*)"')
_MODULE_RE = re.compile(r'"[^"]*"\s+(?:"([^"]*)"|(\S+))')


def parse_blocks(chunk: str) -> list[dict[str, Any]]:
    """Every `<VST ...>` block with its span, header, class hex and module."""
    lines = chunk.split("\n")
    blocks: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        if lines[index].startswith("<VST "):
            end = next(
                (k for k in range(index + 1, len(lines)) if lines[k].rstrip("\r") == ">"),
                None,
            )
            if end is None:
                break
            header = lines[index]
            class_match = _CLASS_RE.search(header)
            quoted = _QUOTED_RE.search(header)
            module_match = _MODULE_RE.search(header)
            module: str | None = None
            if module_match:
                module = (
                    module_match.group(1)
                    if module_match.group(1) is not None
                    else module_match.group(2)
                ) or None
            blocks.append(
                {
                    "header": header.rstrip("\r"),
                    "class_hex": class_match.group(1).upper() if class_match else None,
                    "name": quoted.group(1) if quoted else None,
                    "module": module,
                    "line_start": index,
                    "line_end": end,
                    "block_text": "\n".join(line.rstrip("\r") for line in lines[index : end + 1]),
                }
            )
            index = end + 1
            continue
        index += 1
    return blocks


def locate_block_span(
    chunk: str,
    *,
    class_hex: str | None,
    module: str | None,
    ordinal: int | None = None,
    matching_fx_count: int | None = None,
) -> TargetedStateSlice:
    """Unique-or-refuse correlation of one block to the target identity."""
    if not class_hex and not module:
        raise TargetSliceAmbiguous(
            "no distinguishing identity evidence (class id/module) to correlate the block",
            detail={"class_hex": class_hex, "module": module},
        )
    blocks = parse_blocks(chunk)
    matching = [
        block
        for block in blocks
        if (class_hex is None or block["class_hex"] == class_hex.upper())
        and (module is None or block["module"] == module)
    ]
    if not matching:
        raise Unreadable(
            "no matching state block found in the chunk",
            detail={"n_vst_blocks": len(blocks), "classes": [b["class_hex"] for b in blocks]},
        )
    if matching_fx_count is not None and matching_fx_count != len(matching):
        raise TargetSliceAmbiguous(
            "the number of matching blocks disagrees with the host's matching FX slots",
            detail={
                "matching_blocks": len(matching),
                "matching_fx_slots": matching_fx_count,
                "classes": [b["class_hex"] for b in matching],
            },
        )
    if len(matching) == 1:
        if ordinal not in (None, 0):
            raise TargetSliceAmbiguous(
                "the host places the target at a later position than the single matching block",
                detail={"ordinal": ordinal, "matching_blocks": 1},
            )
        chosen, chosen_ordinal = matching[0], 0
    else:
        if matching_fx_count is None:
            raise TargetSliceAmbiguous(
                "several blocks match the identity and the host count agreement is missing",
                detail={"matching_blocks": len(matching), "ordinal": ordinal},
            )
        if ordinal is None:
            raise TargetSliceAmbiguous(
                "several blocks match the identity and no ordinal is available",
                detail={"matching_blocks": len(matching)},
            )
        if not 0 <= ordinal < len(matching):
            raise TargetSliceAmbiguous(
                "the ordinal cannot address any matching block",
                detail={"ordinal": ordinal, "matching_blocks": len(matching)},
            )
        chosen, chosen_ordinal = matching[ordinal], ordinal
    return TargetedStateSlice(
        chunk_digest=text_digest(chunk),
        line_start=chosen["line_start"],
        line_end=chosen["line_end"],
        header_line=chosen["header"],
        class_hex=chosen["class_hex"],
        ordinal=chosen_ordinal,
        block_text=chosen["block_text"],
        framing=None,
    )
