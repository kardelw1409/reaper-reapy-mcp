"""Vital provider: state codec (V3 provider contract, ported from verified code).

**Provenance.** Every function below is either a **verbatim port** of a function
that `17 AV-13` verified as host-free inside
`C:\\REAPER\\prompts\\vital_sound_design\\scripts\\_b4_lib.py`, or a documented
adaptation of one. The three host-touching functions of that module
(`_get_chunk`, `_set_chunk`, `_default_ctrl`) and the module-level aliases are
**not ported**; the legacy module is never imported (AUD-005).

Adaptations, each marked at the function:

- `find_vst_block` is **not ported**: it selected the first `<VST >` block of a
  chunk, which is the POC defect AUD-001. In V3 the codec receives a
  `TargetedStateSlice` whose span was located by the bridge, so
  `parse_block_framing` parses the block it was handed and has no notion of
  "first".
- `rebuild_chunk` becomes `rebuild_block_text`: it returns the block's own text
  (the bridge splices it into the chunk), never a whole chunk.
- `export_document` is **not ported**: it writes a caller-supplied path, and V1
  has no export op (AUD-009 defers that surface).

The codec is host-free, stdlib-only and slice-exact: `decode` reads the slice's
own bytes, `encode_slice` returns new bytes for exactly that span.
"""

from __future__ import annotations

import base64
import copy
import json
import struct
from dataclasses import dataclass
from typing import Any

from ...errors import InvalidValue, Unreadable
from ...model import (
    StateWritePlan,
    StateWriteUnit,
    TargetedStateSlice,
    VerificationPolicy,
    VerificationReport,
)

__all__ = [
    "VITAL_CODEC_NAME",
    "MUTABLE_ROOT",
    "ROUTE_SLOTS",
    "ROUTE_TABLE_KEY",
    "ROUTE_PAIR_FIELDS",
    "DERIVED_LEAVES",
    "RoutePairField",
    "parse_route_pair_path",
    "b64d",
    "parse_block_framing",
    "decode_document",
    "encode_item_b",
    "encode_item_a",
    "wrap_lines",
    "rebuild_block_text",
    "dig",
    "set_path",
    "flat_paths",
    "list_routings",
    "find_free_slot",
    "make_routing",
    "clear_routing",
    "VitalCodec",
]

VITAL_CODEC_NAME = "vital_base64_json_v1"

#: The declared document root a state write may address (10 §1.2.3: an unrooted
#: path writes where the plugin never looks, which looks exactly like a write
#: that did not land).
MUTABLE_ROOT = "settings"

#: The route table's key inside the document settings (measured layout). The
#: pair fields below are the same two strings `make_routing`/`clear_routing`
#: write, so a guard can be derived from the code that owns the representation
#: instead of from a hand-typed name list.
ROUTE_TABLE_KEY = "modulations"

#: The slot fields that carry a ``(source, destination)`` pair.
ROUTE_PAIR_FIELDS = ("source", "destination")


@dataclass(frozen=True)
class RoutePairField:
    """One parsed ``settings.modulations.<index>.<field>`` path."""

    path: str
    index: int
    field: str


def parse_route_pair_path(path: Any) -> RoutePairField | None:
    """The pair field a dotted path addresses, or ``None``.

    Recognises exactly the shape the codec writes through the state channel
    (``settings.modulations.7.destination``). It is deliberately the codec's own
    representation: a caller can reach the same setting by writing the state
    path directly, so a guard that only looked at route operations would be
    bypassable.
    """
    text = str(path)
    parts = text.split(".")
    if len(parts) != 4 or parts[0] != MUTABLE_ROOT or parts[1] != ROUTE_TABLE_KEY:
        return None
    if parts[3] not in ROUTE_PAIR_FIELDS:
        return None
    if not parts[2].isdigit():
        return None
    return RoutePairField(path=text, index=int(parts[2]), field=parts[3])


#: Vital's modulation matrix size (measured: `settings.modulations` has 64 slots).
ROUTE_SLOTS = 64

#: Leaves the **plugin** re-derives when it serializes its state, measured live
#: (P8, 2026-10-10): `settings.sample.samples` is an 88200-byte base64 sample
#: buffer that the plugin re-encodes/processes itself. Writing a captured value
#: for it does not land - the plugin's own serialization comes back - so
#: 1. a restore must not ask for it (otherwise every restore/rollback fails
#:    verification on a leaf that was never the layer's to write), and
#: 2. an explicit request naming it is refused **before** any write, because a
#:    write whose landing cannot be verified must not be attempted.
#: The exclusion is reported (`restore_changes` returns it) so an outcome can
#: name it instead of quietly claiming a complete restore.
DERIVED_LEAVES: tuple[str, ...] = ("settings.sample.samples",)

_ROUTE_FIELDS = ("amount", "bipolar", "bypass", "power", "stereo")


# ---------------------------------------------------------------------------
# Framing (port of _b4_lib: _b64d, parse_framing, decode_document)
# ---------------------------------------------------------------------------


def b64d(text: str) -> bytes:
    """Port of `_b4_lib._b64d` (AV-13 line 92). base64 with padding repair."""
    return base64.b64decode(text + "=" * (-len(text) % 4))


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def parse_block_framing(block_text: str) -> dict[str, Any]:
    """Adaptation of `_b4_lib.parse_framing` (AV-13 line 115) to one block.

    Measured layout (recorded in `b4_state_blob.json` and 02 §7): the block's
    second line is item A (44 bytes, base64); the lines up to the terminating
    `>` are item B, whose base64 is the clean encoding of the state **plus a
    short artifact REAPER appends** (measured: 8 chars beyond the clean
    encoding). Reproducing that artifact verbatim is what makes a rebuild
    byte-identical; dropping it produced the POC's recorded 1-line, 9-char
    difference.

    **Adaptation:** the true length of item B is taken from the length field
    item A carries (`A[0x20:0x24]`, measured as ``len(item B)``) instead of
    inferring it from the re-encoded base64 length. That is what makes the
    appended artifact identifiable by construction rather than by luck: the
    joined base64 decodes to item B *plus* artifact bytes, and the recorded
    length says exactly where item B ends.
    """
    lines = block_text.split("\n")
    if not lines or not lines[0].startswith("<VST "):
        raise Unreadable("the slice does not start with a VST block header", detail={})
    end = next((k for k in range(1, len(lines)) if lines[k].rstrip("\r") == ">"), None)
    if end is None:
        raise Unreadable("the slice's VST block is not terminated", detail={})
    if end < 3:
        raise Unreadable("the VST block is too short to carry item A and item B", detail={"lines": end})
    item_a = lines[1].rstrip("\r")
    b_lines = [line.rstrip("\r") for line in lines[2:end]]
    item_b = "".join(b_lines)
    A = b64d(item_a)
    decoded = b64d(item_b)
    declared = struct.unpack("<I", A[0x20:0x24])[0] if len(A) >= 0x24 else 0
    if 0 < declared <= len(decoded):
        B = decoded[:declared]
    else:
        B = decoded
    clean = _b64e(B)
    artifact = item_b[len(clean):] if item_b.startswith(clean) else ""
    return {
        "header": lines[0].rstrip("\r"),
        "tail_lines": lines[end:],
        "item_a": item_a,
        "b_lines": b_lines,
        "A": A,
        "B": B,
        "declared_len_b": declared or None,
        "b64_artifact": artifact,
        "b64_clean_len": len(clean),
        "b64_width": max((len(line) for line in b_lines), default=0),
        "line_end": end,
    }


def _decode_at(B: bytes, brace: int) -> tuple[dict[str, Any], int] | None:
    """Decode a JSON object starting at this brace; ``None`` when nothing does.

    Returns the object **and the byte length** of its JSON text, so callers slice
    bytes and characters consistently. The tail is decoded with
    ``surrogateescape``: item B ends in binary data, and a document is decodable
    even when the bytes *after* its JSON are not valid UTF-8.
    """
    text = B[brace:].decode("utf-8", "surrogateescape")
    try:
        obj, end = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return None
    consumed = len(text[:end].encode("utf-8", "surrogateescape"))
    return obj, consumed


def decode_document(B: bytes) -> tuple[dict[str, Any], int]:
    """Locate the document: the first brace whose object carries ``settings``.

    Neither single-rule locator survives the measured shapes (P8):

    - searching for the literal ``b'{"author'`` fails on a **padded** document
      (the length-preserving rewrite inserts whitespace) and then found a
      *nested* JSON string inside the state - a wavetable document that also
      begins with an ``author`` key;
    - taking the first ``{`` fails on the recorded fixture, whose binary item-B
      prefix contains a ``0x7b`` byte.

    So the candidates are scanned in ascending order and the first one that
    decodes to a **dict carrying a ``settings`` object** wins - the invariant the
    codec already enforces downstream, and a shape neither the binary prefix nor
    a nested wavetable can fake. The outer document precedes any nested string
    inside it, so order alone decides, and a state without any such object still
    fails loudly (either "no JSON document" or the plain first decodable dict, so
    the caller's specific error keeps its meaning).
    """
    first_dict: tuple[dict[str, Any], int] | None = None
    start = 0
    while True:
        brace = B.find(b"{", start)
        if brace < 0:
            break
        decoded = _decode_at(B, brace)
        if decoded is not None:
            obj, consumed = decoded
            if isinstance(obj, dict):
                if isinstance(obj.get("settings"), dict):
                    return obj, brace
                if first_dict is None:
                    first_dict = (obj, brace)
        start = brace + 1
    if first_dict is not None:
        return first_dict
    raise Unreadable(
        "no decodable JSON object inside the VST state", detail={"bytes": len(B)}
    )


def encode_item_b(B: bytes, obj: dict[str, Any], mode: str = "auto") -> bytes:
    """Port of `_b4_lib.encode_item_b` (AV-13 line 175), with the pad point corrected.

    `pad` (the provably lossless case) inserts whitespace **before the closing
    brace** so both recorded length fields stay valid; `grow` rewrites the
    length field inside item B. `auto` pads when it fits and grows when the
    document gets bigger (creating a route always grows).

    **Why the insertion point moved (P8 finding).** The ported version padded
    right after the opening brace, which removed the ``{"author`` marker from the
    document's own bytes; a live run then decoded a *nested* JSON string (a
    wavetable document that also starts with an ``author`` key) instead of the
    state. Both positions are legal JSON; the one that leaves the document's
    opening bytes untouched is the one that cannot mislead a reader.
    """
    _old, brace = decode_document(B)
    decoded = _decode_at(B, brace)
    if decoded is None:  # pragma: no cover - decode_document just validated this brace
        raise Unreadable("the document could not be re-decoded for the rewrite", detail={"brace": brace})
    _o, consumed = decoded
    tail = B[brace + consumed:]
    old_json = B[brace:brace + consumed]
    new_json = json.dumps(obj, separators=(",", ":")).encode("utf-8")

    delta = len(old_json) - len(new_json)
    if mode in ("pad", "auto") and delta >= 0:
        if delta:
            # The padding goes **before the closing brace**, not after the opening
            # one (P8 finding): whitespace there is equally valid JSON and equally
            # length-preserving, and it leaves the document's opening bytes - and
            # with them the ``{"author`` marker the ported heuristic looked for -
            # exactly where a consumer expects them.
            if not new_json.endswith(b"}"):
                raise Unreadable(
                    "the serialized document does not end with a closing brace, so the "
                    "length-preserving padding has nowhere safe to go",
                    detail={"tail": new_json[-8:].decode("utf-8", "replace")},
                )
            new_json = new_json[:-1] + b" " * delta + b"}"
        return B[:brace] + new_json + tail
    if mode == "pad":
        raise InvalidValue(
            f"the document grew by {-delta} bytes; padding cannot compensate",
            detail={"delta": -delta, "mode": mode},
        )
    out = B[:brace] + new_json + tail
    if len(out) >= 4:
        out = struct.pack("<I", len(out) - 16) + out[4:]
    return out


def encode_item_a(A: bytes, b_len: int) -> bytes:
    """Port of `_b4_lib.encode_item_a` (AV-13 line 231).

    The length field sits at ``+0x20`` (measured); writing it at ``+0x28``
    produced the POC's recorded first difference at char 572.
    """
    if len(A) >= 0x24:
        A = A[:0x20] + struct.pack("<I", b_len) + A[0x24:]
    return A


def wrap_lines(b64: str, width: int = 280) -> list[str]:
    """Port of `_b4_lib.wrap_lines` (AV-13 line 256)."""
    return [b64[k:k + width] for k in range(0, len(b64), width)]


def rebuild_block_text(framing: dict[str, Any], obj: dict[str, Any], *, mode: str = "auto") -> str:
    """Adaptation of `_b4_lib.rebuild_chunk` (AV-13 line 260) to one block.

    Unchanged text is carried over verbatim: the header line, item A, the
    wrap width, and the recorded base64 artifact are all reproduced, so a
    no-op rebuild is byte-identical to the input.
    """
    B = encode_item_b(framing["B"], obj, mode=mode)
    A = encode_item_a(framing["A"], len(B))
    b_b64 = _b64e(B)
    lines = [framing["header"], _b64e(A)]
    lines.extend(wrap_lines(b_b64, framing.get("b64_width") or 280))
    artifact = framing.get("b64_artifact") or ""
    if artifact:
        lines.append(artifact)
    lines.extend(framing["tail_lines"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Document paths (port of _b4_lib._dig; the setter and flattener are new)
# ---------------------------------------------------------------------------


def _segments(path: str) -> list[str]:
    return [segment for segment in str(path).split(".") if segment != ""]


def dig(doc: Any, path: str) -> Any:
    """Port of `_b4_lib._dig` (AV-13 line 377): numeric segments index lists."""
    current = doc
    for part in _segments(path):
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(current, dict):
            if part not in current:
                return None
            current = current[part]
        else:
            return None
    return current


def _check_root(path: str) -> None:
    head = _segments(path)[:1]
    if head != [MUTABLE_ROOT]:
        raise InvalidValue(
            f"state path {path!r} is not rooted at {MUTABLE_ROOT!r}; an unrooted path "
            "writes where the plugin never looks",
            detail={"path": path, "roots": [MUTABLE_ROOT]},
        )


def _check_not_derived(path: str) -> None:
    """A leaf the plugin re-derives cannot be written: refuse before any write."""
    if path in DERIVED_LEAVES:
        raise InvalidValue(
            f"state path {path!r} is plugin-derived: the plugin re-serializes it from its "
            "own buffer, so a written value does not land and cannot be verified",
            detail={
                "path": path,
                "reason": "derived_leaf_not_writable",
                "derived_leaves": list(DERIVED_LEAVES),
            },
        )


def set_path(doc: dict[str, Any], path: str, value: Any) -> None:
    """Set a rooted dotted path; numeric segments index lists. Refuses unrooted."""
    _check_root(path)
    parts = _segments(path)
    current: Any = doc
    for index, part in enumerate(parts[:-1]):
        nxt = parts[index + 1]
        if isinstance(current, list):
            try:
                position = int(part)
            except ValueError as exc:
                raise InvalidValue(
                    f"path {path!r} indexes a list with a non-numeric segment {part!r}",
                    detail={"path": path},
                ) from exc
            if position >= len(current):
                raise InvalidValue(
                    f"path {path!r} indexes beyond the end of a list; V1 does not extend documents",
                    detail={"path": path, "length": len(current)},
                )
            current = current[position]
            continue
        if not isinstance(current, dict):
            raise InvalidValue(f"path {path!r} descends into a scalar", detail={"path": path})
        child = current.get(part)
        if child is None:
            child = [] if nxt.isdigit() else {}
            current[part] = child
        current = child
    last = parts[-1]
    if isinstance(current, list):
        try:
            position = int(last)
        except ValueError as exc:
            raise InvalidValue(f"path {path!r} ends on a non-numeric list index", detail={"path": path}) from exc
        if position >= len(current):
            raise InvalidValue(
                f"path {path!r} indexes beyond the end of a list",
                detail={"path": path, "length": len(current)},
            )
        current[position] = value
        return
    if not isinstance(current, dict):
        raise InvalidValue(f"path {path!r} ends on a scalar", detail={"path": path})
    current[last] = value


def flat_paths(value: Any, prefix: str = "") -> dict[str, Any]:
    """Every leaf of a document as ``dotted.path`` (numeric list indices)."""
    leaves: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, sub in value.items():
            leaves.update(flat_paths(sub, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        for index, sub in enumerate(value):
            leaves.update(flat_paths(sub, f"{prefix}.{index}" if prefix else str(index)))
    else:
        leaves[prefix] = value
    return leaves


# ---------------------------------------------------------------------------
# Routes (port of _b4_lib list_routings / find_free_slot / make_routing / clear_routing)
# ---------------------------------------------------------------------------


def _settings(doc: dict[str, Any]) -> dict[str, Any]:
    settings = doc.get("settings")
    if not isinstance(settings, dict):
        raise Unreadable("the Vital document has no settings object", detail={})
    return settings


def _modulations(settings: dict[str, Any]) -> list[dict[str, Any]]:
    mods = settings.get(ROUTE_TABLE_KEY)
    if not isinstance(mods, list):
        mods = [{"destination": "", "source": ""} for _ in range(ROUTE_SLOTS)]
        settings[ROUTE_TABLE_KEY] = mods
    while len(mods) < ROUTE_SLOTS:
        mods.append({"destination": "", "source": ""})
    return mods


def list_routings(doc: dict[str, Any], *, include_empty: bool = False) -> dict[int, dict[str, Any]]:
    """Port of `_b4_lib.list_routings` (AV-13 line 396), keyed by 1-based slot.

    ``include_empty=True`` also returns the empty slots: the universal route
    table (P6) addresses slots, so an empty slot must be visible as an empty
    slot rather than missing.
    """
    settings = _settings(doc)
    mods = _modulations(settings)
    out: dict[int, dict[str, Any]] = {}
    for index, slot in enumerate(mods):
        if not isinstance(slot, dict):
            continue
        source = str(slot.get("source") or "").strip()
        destination = str(slot.get("destination") or "").strip()
        if not source and not destination and not include_empty:
            continue
        number = index + 1
        row: dict[str, Any] = {"slot": number, "source": source, "destination": destination}
        for field in _ROUTE_FIELDS:
            row[field] = settings.get(f"modulation_{number}_{field}")
        out[number] = row
    return out


def find_free_slot(doc: dict[str, Any]) -> int | None:
    """Port of `_b4_lib.find_free_slot` (AV-13 line 421)."""
    settings = _settings(doc)
    mods = _modulations(settings)
    for index, slot in enumerate(mods):
        if isinstance(slot, dict) and not str(slot.get("source") or "").strip() and not str(
            slot.get("destination") or ""
        ).strip():
            return index + 1
    return None


def make_routing(
    doc: dict[str, Any],
    source: str,
    destination: str,
    amount: float,
    slot: int | None = None,
    bipolar: float = 0.0,
    stereo: float = 0.0,
    power: float = 1.0,
    bypass: float = 0.0,
) -> dict[str, Any]:
    """Port of `_b4_lib.make_routing` (AV-13 line 431).

    One route per call - the declared structural limit (`one_route_per_state_write`)
    is expressed by the planner, not here.
    """
    settings = _settings(doc)
    mods = _modulations(settings)
    number = slot or find_free_slot(doc)
    if not number:
        raise InvalidValue(
            "no free modulation slot (all 64 in use)",
            detail={"slots": ROUTE_SLOTS, "reason": "no_free_slot"},
        )
    if not 1 <= int(number) <= len(mods):
        raise InvalidValue(
            f"route slot {number} is outside 1..{len(mods)}",
            detail={"slot": number, "slots": len(mods)},
        )
    index = int(number) - 1
    mods[index] = {"destination": destination, "source": source}
    settings[f"modulation_{number}_amount"] = float(amount)
    settings[f"modulation_{number}_bipolar"] = float(bipolar)
    settings[f"modulation_{number}_stereo"] = float(stereo)
    settings[f"modulation_{number}_power"] = float(power)
    settings[f"modulation_{number}_bypass"] = float(bypass)
    return {
        f"{MUTABLE_ROOT}.{ROUTE_TABLE_KEY}.{index}.source": source,
        f"{MUTABLE_ROOT}.{ROUTE_TABLE_KEY}.{index}.destination": destination,
        f"settings.modulation_{number}_amount": float(amount),
    }


def clear_routing(doc: dict[str, Any], slot: int) -> dict[str, Any]:
    """Port of `_b4_lib.clear_routing` (AV-13 line 463)."""
    settings = _settings(doc)
    mods = _modulations(settings)
    index = int(slot) - 1
    if index < len(mods):
        mods[index] = {"destination": "", "source": ""}
    settings[f"modulation_{slot}_amount"] = 0.0
    return {
        f"{MUTABLE_ROOT}.{ROUTE_TABLE_KEY}.{index}.source": "",
        f"{MUTABLE_ROOT}.{ROUTE_TABLE_KEY}.{index}.destination": "",
        f"settings.modulation_{slot}_amount": 0.0,
    }


# ---------------------------------------------------------------------------
# The codec
# ---------------------------------------------------------------------------


class VitalCodec:
    """`StateCodec` for Vital's base64-JSON document inside one located block.

    Batching: all non-route path changes ride one unit; **every route operation
    gets its own unit** (the declared structural limit). A create resolves its
    free slot at encode time from the document it is handed - i.e. from the
    read-back of the previous unit - and records it in the unit payload so
    verification addresses the slot that was actually written.
    """

    name = VITAL_CODEC_NAME

    # -- document <-> block -------------------------------------------------

    def decode(self, slice: TargetedStateSlice) -> dict[str, Any]:
        framing = slice.framing if isinstance(slice.framing, dict) and "B" in slice.framing else None
        if framing is None:
            framing = parse_block_framing(slice.block_text)
        document, _brace = decode_document(framing["B"])
        routes = list_routings(document, include_empty=True)
        return {
            "document": document,
            "settings": dict(document.get("settings") or {}),
            "routes": list(routes.values()),
            "synth_version": document.get("synth_version"),
            "preset_name": document.get("preset_name"),
            "framing": _framing_summary(framing),
        }

    def plan_state_writes(self, doc: dict[str, Any], changes: dict[str, Any]) -> StateWritePlan:
        paths = dict(changes.get("paths") or {})
        units: list[StateWriteUnit] = []
        if paths:
            for path in paths:
                _check_root(str(path))
                _check_not_derived(str(path))
            units.append(StateWriteUnit(order_index=0, kind="state", payload={"paths": paths}))
        route_ops = (
            [{"op": "create", **spec} for spec in (changes.get("routes_create") or [])]
            + [{"op": "update", **spec} for spec in (changes.get("routes_update") or [])]
            + [{"op": "remove", **spec} for spec in (changes.get("routes_remove") or [])]
        )
        for op in route_ops:
            units.append(
                StateWriteUnit(order_index=len(units), kind="state", payload={"route_op": op})
            )
        return StateWritePlan(
            units=units,
            rollback_plan=[
                {
                    "kind": "state",
                    "why": "restore every captured settings leaf, then the parameter vector",
                }
            ],
            verification_plan={
                "space": "native",
                "tolerances": {"state.float": 1e-05},
                "per_unit": "decode the located slice and compare only the written fields",
            },
        )

    def encode_slice(
        self, doc: dict[str, Any], unit: StateWriteUnit, current: TargetedStateSlice
    ) -> TargetedStateSlice:
        framing = parse_block_framing(current.block_text)
        document = copy.deepcopy(doc["document"] if isinstance(doc, dict) and "document" in doc else doc)
        payload = unit.payload or {}
        for path, value in (payload.get("paths") or {}).items():
            try:
                set_path(document, str(path), value)
            except InvalidValue:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad path is a typed refusal
                raise InvalidValue(
                    f"state path {path!r} could not be written: {exc}", detail={"path": path}
                ) from exc
        route_op = payload.get("route_op")
        if route_op:
            self._apply_route_op(document, route_op, payload)
        block_text = rebuild_block_text(framing, document)
        return TargetedStateSlice(
            chunk_digest=current.chunk_digest,
            line_start=current.line_start,
            line_end=current.line_end,
            header_line=current.header_line,
            class_hex=current.class_hex,
            ordinal=current.ordinal,
            block_text=block_text,
            framing=parse_block_framing(block_text),
        )

    def verify(
        self, readback: dict[str, Any], unit: StateWriteUnit, policy: VerificationPolicy
    ) -> VerificationReport:
        tolerance = float(policy.tolerances.get("state.float", 1e-05))
        document = readback["document"] if isinstance(readback, dict) and "document" in readback else readback
        routes = {row["slot"]: row for row in list_routings(document, include_empty=True).values()}
        payload = unit.payload or {}
        rows: list[dict[str, Any]] = []
        for path, value in (payload.get("paths") or {}).items():
            actual = dig(document, str(path))
            rows.append(
                {
                    "path": str(path),
                    "written": value,
                    "read_back": actual,
                    "agrees": _agrees(value, actual, tolerance),
                    "channel": "state",
                }
            )
        route_op = payload.get("route_op")
        if route_op:
            rows.extend(self._verify_route_op(route_op, payload, routes, tolerance))
        mismatched = [row for row in rows if not row["agrees"]]
        return VerificationReport(
            ok=bool(rows) and not mismatched,
            rows=rows,
            mismatched=mismatched,
            comparators=dict(policy.comparators),
            tolerances=dict(policy.tolerances),
            space="native",
        )

    def snapshot_document(self, doc: dict[str, Any]) -> dict[str, Any]:
        """The restorable part: the whole document (V1 writes only touch it)."""
        document = doc["document"] if isinstance(doc, dict) and "document" in doc else doc
        return {"document": copy.deepcopy(document), "root": MUTABLE_ROOT}

    def restore_changes(self, doc: dict[str, Any], snap: dict[str, Any]) -> dict[str, Any]:
        """Restoration as document changes: every captured settings leaf.

        Expressed as `paths` executed through `plan_state_writes`, never as a blob
        write (08 §3), so the rollback travels the same verified unit path as any
        other change.

        Leaves the plugin derives itself (`DERIVED_LEAVES`) are **excluded**: a
        write of a captured value for them cannot land (measured live), and a
        restore that demanded one would fail verification for a leaf that was
        never writable. The excluded paths are returned alongside the change set
        so Core can name them in the outcome instead of claiming more than it
        restored.
        """
        document = snap["document"]
        settings = document.get("settings") or {}
        leaves = flat_paths({MUTABLE_ROOT: settings})
        excluded = [path for path in DERIVED_LEAVES if path in leaves]
        for path in excluded:
            leaves.pop(path, None)
        return {"paths": leaves, "derived_excluded": excluded}

    # -- internals ----------------------------------------------------------

    def _apply_route_op(
        self, document: dict[str, Any], route_op: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        kind = route_op.get("op")
        if kind == "create":
            amount = route_op.get("amount")
            if amount is None:
                raise InvalidValue(
                    "a create needs a native amount (convert the semantic depth first)",
                    detail={"route": route_op, "reason": "missing_amount"},
                )
            slot = find_free_slot(document)
            if slot is None:
                raise InvalidValue(
                    "no free modulation slot", detail={"slots": ROUTE_SLOTS, "reason": "no_free_slot"}
                )
            make_routing(
                document,
                str(route_op.get("source") or ""),
                str(route_op.get("destination") or ""),
                float(amount),
                slot=slot,
                bipolar=float(route_op.get("bipolar") or 0.0),
            )
            # recorded for verify(): the slot this unit actually wrote, resolved
            # against the document the engine handed over (the previous unit's
            # read-back), never against a plan-time snapshot
            payload["resolved_slot"] = slot
            return
        slot = route_op.get("slot")
        if slot is None:
            raise InvalidValue(
                f"a {kind} must name the route's slot identity",
                detail={"route": route_op, "reason": "missing_slot"},
            )
        if kind == "remove":
            clear_routing(document, int(slot))
            payload["resolved_slot"] = int(slot)
            return
        if kind == "update":
            settings = _settings(document)
            number = int(slot)
            mods = _modulations(settings)
            index = number - 1
            if kind == "update" and not (0 <= index < len(mods)):
                raise InvalidValue(
                    f"route slot {slot} does not exist", detail={"slot": slot, "slots": len(mods)}
                )
            written = False
            for key in ("source", "destination"):
                if route_op.get(key) is not None:
                    mods[index][key] = str(route_op[key])
                    written = True
            for field in _ROUTE_FIELDS:
                value = route_op.get(field)
                if value is None:
                    continue
                if field == "amount":
                    settings[f"modulation_{number}_amount"] = float(value)
                else:
                    settings[f"modulation_{number}_{field}"] = float(value)
                written = True
            if not written:
                raise InvalidValue(
                    "an update names no field to change",
                    detail={"route": route_op, "reason": "no_fields"},
                )
            payload["resolved_slot"] = number
            return
        raise InvalidValue(f"unknown route operation {kind!r}", detail={"op": kind})

    def _verify_route_op(
        self,
        route_op: dict[str, Any],
        payload: dict[str, Any],
        routes: dict[int, dict[str, Any]],
        tolerance: float,
    ) -> list[dict[str, Any]]:
        kind = route_op.get("op")
        slot = payload.get("resolved_slot")
        if slot is None:
            # a create that was never encoded leaves nothing to verify
            return [
                {
                    "subject": {"route": None},
                    "written": kind,
                    "read_back": None,
                    "agrees": False,
                    "channel": "state",
                    "reason": "the unit did not resolve a slot",
                }
            ]
        landed = routes.get(int(slot))
        if kind == "remove":
            agrees = bool(landed) and not landed.get("destination") and not landed.get("source")
            return [
                {
                    "subject": {"route": int(slot)},
                    "written": "removed",
                    "read_back": landed,
                    "agrees": agrees,
                    "channel": "state",
                }
            ]
        rows: list[dict[str, Any]] = []
        if not landed:
            return [
                {
                    "subject": {"route": int(slot)},
                    "written": "present",
                    "read_back": None,
                    "agrees": False,
                    "channel": "state",
                }
            ]
        fields = {
            key: value
            for key, value in route_op.items()
            if key not in ("op", "slot") and value is not None
        }
        for key, value in fields.items():
            actual = landed.get(key)
            rows.append(
                {
                    "subject": {"route": int(slot)},
                    "field": key,
                    "written": value,
                    "read_back": actual,
                    "agrees": _agrees(value, actual, tolerance),
                    "channel": "state",
                }
            )
        return rows


def _framing_summary(framing: dict[str, Any]) -> dict[str, Any]:
    """What may travel in a DocumentView: sizes and widths, never the bytes."""
    return {
        "item_a_bytes": len(framing["A"]),
        "item_b_bytes": len(framing["B"]),
        "b64_width": framing.get("b64_width"),
        "b64_artifact_len": len(framing.get("b64_artifact") or ""),
    }


def _agrees(written: Any, read_back: Any, tolerance: float) -> bool:
    if isinstance(written, bool) or isinstance(read_back, bool):
        return written == read_back
    if isinstance(written, (int, float)) and isinstance(read_back, (int, float)):
        return abs(float(written) - float(read_back)) <= tolerance
    return written == read_back
