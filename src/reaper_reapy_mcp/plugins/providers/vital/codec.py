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

import array
import base64
import copy
import json
import math
import struct
from dataclasses import dataclass
from typing import Any, Mapping

from ...errors import InvalidValue, PluginError, Unreadable
from ...model import (
    StateWritePlan,
    StateWriteUnit,
    TargetedStateSlice,
    VerificationPolicy,
    VerificationReport,
)
from . import wavetables as wavetables_module

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

#: Top-level metadata keys a preset carries OUTSIDE `settings` (measured corpus
#: layout). They are writable labels - measured live 2026-10-17 (P38): all eight
#: land and read back exactly while the write stays inside `BATCH_GROWTH_BUDGET`.
LABEL_PATHS: tuple[str, ...] = (
    "preset_name",
    "author",
    "comments",
    "preset_style",
    "macro1",
    "macro2",
    "macro3",
    "macro4",
)

#: The track-chunk TEXT a state may occupy before REAPER's own chunk read gives up on it.
#:
#: MEASURED (2026-10-18, live, two states on one track - one readable, one not):
#:
#:   * document 3,048,473 chars -> a COMPLETE 4,083,633-character chunk, returned identically at
#:     every capacity at or above its length (so it was not truncated), decoded and verified;
#:   * document 3,127,179 chars -> a COMPLETE 4,189,248-character chunk (the largest measured
#:     READABLE state; `_p40m_ceiling_probe.py`), while 3,166,642 (~4.24 M) and 3,206,106
#:     (~4.29 M) both faulted - so the line sits between 4,189,248 and 4,240,000 characters;
#:   * document 3,363,963 chars -> chunk 4,485,284, and `GetTrackStateChunk` answered
#:     `OSError: exception: access violation writing 0x...` three runs in a row, while the same
#:     capacities read light tracks (1,176 chars) fine.
#:
#: So the host's chunk channel dies somewhere between 4.08 and 4.49 MB of text, and the figure below
#: is the largest text MEASURED READABLE rather than a guessed buffer size. A state past it risks an
#: instance that no read can reach again - and the read is the first step of every later operation
#: (verification, rollback). A capacity BELOW the real length is not a safe fallback either: it
#: returns exactly `capacity - 1` characters, i.e. a silently truncated chunk.
STATE_CHUNK_CEILING_CHARS = 4_100_000

#: The chunk carries the document once as base64; measured chunk/document ratios are 1.3333 (the
#: faulting state) and 1.3396 (the readable one), so the plain base64 step is the right model.
STATE_CHUNK_EXPANSION = 4 / 3

#: A small allowance for whatever else the track's chunk carries around the plugin block (name,
#: items, envelopes, the wrapper). The measured readable case had 16,367 chars of slack under the
#: ceiling, so this margin keeps a projection from planning right onto the measured edge.
STATE_CEILING_MARGIN_CHARS = 25_000


#: The sample object's key inside `settings` and its whole-block path.
SAMPLE_BLOCK_KEY = "sample"
SAMPLE_PATH = f"{MUTABLE_ROOT}.{SAMPLE_BLOCK_KEY}"

#: The keys of a sample block that carry PCM (`Sample::stateToJson` writes `samples_stereo`
#: when the buffer is stereo). Both are payloads: both are re-encoded by the plugin, so both are
#: verified through the measured band and neither is compared as an ordinary field.
SAMPLE_PAYLOAD_KEYS: tuple[str, ...] = ("samples", "samples_stereo")

#: The host's own trailer inside item B, in bytes (measured, P39). Item B is REAPER's
#: "VstW" wrapper: a 16-byte header, a chunk magic, the plugin's JSON, a 40-byte trailer of
#: which the first 32 bytes are inside the plugin-state region ("CcnK", whose own size field
#: is `len(item B) - 40`) and the last 8 are the host's.
HOST_TRAILER_BYTES = 8

#: The characters that follow the document INSIDE the plugin's state - the headroom a state
#: write may grow the document by, measured (P39, 2026-10-17, 1.5.5).
#:
#: The rule the whole channel obeys, measured exactly on plain string fields with the block
#: delta held constant: a write LANDS iff ``json_end_new <= len(item B, before) - 8``.
#: Equivalently the document may grow by the bytes that follow its JSON inside the region the
#: host hands the plugin - 32 characters in every state measured, because the trailer after
#: the JSON is 40 bytes and the host keeps its last 8. Measured ladder (probe
#: `probe_p39_wrapper_sizes.py`, evidence `…20261017T211512Z…`): +8/+16/+24/+31 land, +32
#: lands EXACTLY on the bound, +33/+40/+64 are ignored and leave the state byte-identical.
#: Patching item B's own size fields does NOT move the bound (measured), so the number the
#: host uses is its own remembered length, not a field in the text we send.
STATE_HEADROOM_BYTES = 32

#: Room kept unused so an estimate that undershoots by a few characters cannot silently miss
#: the bound above. Every measured batch was packed with the resulting number.
HEADROOM_MARGIN_BYTES = 8

#: The per-unit GROWTH BUDGET of one state write (serialized-JSON characters per unit).
#:
#: P39 corrected what this number IS. It was originally read off the BLOCK-TEXT delta (+44
#: lands, +48 does not) and treated as an estimate-conversion problem (real ~= 1.4 x estimate,
#: see `GROWTH_ESTIMATE_TO_REAL`). Both numbers were the same 32-character DOCUMENT bound seen
#: through base64 expansion and wrapping: the block delta of a 32-character growth is +44, and
#: a 33-character growth has the SAME +44 - which is why the boundary looked like a band.
#: The budget is therefore the measured headroom minus a margin, and the live value is
#: `decode()["framing"]["headroom_bytes"]` when the framing is available.
BATCH_GROWTH_BUDGET = STATE_HEADROOM_BYTES - HEADROOM_MARGIN_BYTES

#: Safety cap on paths per batch. The BUDGET, not the count, is the measured
#: constraint; the cap only stops a pathological single batch.
BATCH_MAX_PATHS = 60

#: The measured conversion from a per-path ESTIMATE to the real BLOCK-TEXT delta
#: (2026-10-17, `probe_p38_batch_calibration.py` `…194156Z…`: single controls estimated
#: at 14..16 landed at 20..24). Kept because the calibration is still what a block-text
#: delta is predicted with; it is NOT what the landing rule is made of (see P39):
#: `BATCH_GROWTH_BUDGET` is a document-growth budget, not an estimate to convert.
GROWTH_ESTIMATE_TO_REAL = 1.4

#: The largest real block-text delta measured to LAND (and the smallest measured to be
#: ignored is 48): the block-text face of the P39 bound.
MEASURED_LANDING_BAND = 44

#: The growth ONE path may carry on its own (the same unit and value as `BATCH_GROWTH_BUDGET`).
#:
#: No batching can rescue a single path bigger than the headroom, and the measured outcome of
#: sending one is a SILENT ignore - the write is accepted, the block is rejected, the value
#: reads back unchanged - so such a path is named as a gap instead of failing a transaction
#: that would then roll back everything that DID land. Measured 2026-10-17 (P38 acceptance +
#: calibration): 7 element arrays (`settings.lfos.N.points`/`.powers`, estimated +140..+372)
#: and a 725-character `comments` label all read back UNCHANGED. A WAVETABLE slot is the one
#: exception: it is deliverable by opening room first (`balloon`), so it is planned, not
#: dropped, when it grows past this.
SINGLE_PATH_GROWTH_LIMIT = BATCH_GROWTH_BUDGET


#: The delay the plugin introduces into the sample payload on EVERY state load
#: (measured repeatedly 2026-10-16/17: the read-back payload equals the written
#: one shifted by exactly 4 samples, the first 4 zeroed). A restore pre-advances
#: the captured payload by this much, so the landed sample reproduces the
#: captured timing exactly (measured: byte-exact restore of the sample slot).
SAMPLE_RELOAD_DELAY = 4

#: The `length` a sample BALLOON asks for, as a multiple of the file's own sample count.
#: The plugin re-serializes 8/3 characters per sample (`Sample::stateToJson` writes int16
#: base64), so 1.2 leaves the file's own payload a comfortable shrink margin to land inside.
SAMPLE_BALLOON_MARGIN = 1.2

#: Samples a sample balloon's OWN payload carries: the file's opening samples, so the seconds the
#: balloon is live play the target's own head rather than the memory the plugin preallocated.
SAMPLE_BALLOON_SAMPLES = 8

#: `Sample::loadSample` clamps its buffer to this (`kMaxSize` in the plugin's source), so a
#: `length` beyond it cannot be honoured - and a file bigger than it cannot be delivered this way.
SAMPLE_MAX_SAMPLES = 1_764_000

#: How much of a written sample payload the plugin's OWN re-encode may move.
#:
#: The plugin stores the buffer as float32 and re-quantizes to int16 when it saves,
#: truncating - so a payload can come back with individual samples moved by exactly
#: one step. **How many samples move is payload-dependent, and that is measured, not
#: assumed:** three recorded applies landed a 44100-sample payload with a residual of
#: ZERO (2026-10-17, `…175859Z…`, `…183221Z…`, `…185301Z…`), while a fourth moved 1060
#: of 44096 samples (2.4 %) by exactly ±1 LSB (`…193527Z…`). The same 2.5 % band was
#: measured on the plugin's own re-serialisation in Stage B (1109 of 44100 samples).
#:
#: So the verdict reads: the payload landed at the measured delay, and every difference
#: from it is the channel's own one-step requantization. A payload that is actually
#: different fails both bounds - it moves more than the band, or by more than one step.
SAMPLE_REQUANT_BAND = 0.05
SAMPLE_REQUANT_STEP = 1

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
    if head != [MUTABLE_ROOT] and path not in LABEL_PATHS:
        raise InvalidValue(
            f"state path {path!r} is not rooted at {MUTABLE_ROOT!r}; an unrooted path "
            "writes where the plugin never looks",
            detail={"path": path, "roots": [MUTABLE_ROOT], "labels": list(LABEL_PATHS)},
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
    power: float = 0.0,
    bypass: float = 0.0,
) -> dict[str, Any]:
    """Port of `_b4_lib.make_routing` (AV-13 line 431).

    One route per call - the declared structural limit (`one_route_per_state_write`)
    is expressed by the planner, not here.

    ``power`` defaults to **0.0**, measured twice on 2026-10-11 rather than
    inherited: a whole-corpus walk (`tests/plugins/live/probe_p19_corpus_pitch.py`,
    2226 presets, 17036 routes) stores ``power = 0.0`` in **16868** of them, and
    every empty slot of the live 1.5.5 instance stores 0.0 while its "Modulation N
    Power" parameter displays "0" at host-normalized 0.5 - i.e. 0.0 is the value
    the document and the plugin both treat as neutral. An earlier default of 1.0
    would have stamped a nonzero curve on every created route (this file learned
    it from the codec fixture, which is not evidence about the plugin).
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
        document, brace = decode_document(framing["B"])
        routes = list_routings(document, include_empty=True)
        return {
            "document": document,
            "settings": dict(document.get("settings") or {}),
            "routes": list(routes.values()),
            "synth_version": document.get("synth_version"),
            "preset_name": document.get("preset_name"),
            "framing": _framing_summary(framing, headroom=_headroom_bytes(framing["B"], brace)),
        }

    def plan_state_writes(
        self, doc: dict[str, Any], changes: dict[str, Any], *, other_track_chars: int = 0
    ) -> StateWritePlan:
        """Plan the state channel as GROWTH-BUDGETED batches (measured law, P38/P39).

        Batching: route operations keep their own unit (declared structural limit);
        a wavetable slot rides its own unit (with a balloon ahead of it when it
        grows); the remaining paths are greedy-packed so each unit's estimated
        serialized growth stays inside the channel's budget.

        The budget is a MEASURED landing bound (P39): a state write lands iff the
        new document is at most `headroom` characters longer than the one the host
        holds (`len(item B) - 8 - the document's end`, 32 in every state measured;
        `decode()` reports the live value). One big unit silently does not land, so
        the batch - not the request size - is what the channel enforces.

        A single path above the budget is NOT attempted: no batching can make it
        land and the measured outcome is a silent ignore, so it is named as a gap
        instead of failing a transaction that would then roll back everything that
        DID land. A WAVETABLE slot is the one exception, and the reason a balloon
        exists: the slot is inflated first (the plugin re-serializes stub keyframes
        as FULL frames, so the state becomes bigger than the table) and then the
        table is written as a SHRINK, which always lands. Measured end to end with
        a 16-frame / 175,497-byte table.

        Readability is budgeted against the WHOLE TRACK, not against this block
        (P42, measured live 2026-10-18): `other_track_chars` is subtracted from the
        measured read ceiling first, so a plan cannot put the track's chunk past
        what the host can return - which is what happened when a second instance on
        the same track was loaded and the chunk became unreadable.
        """
        ceiling = _state_ceiling_chars(other_track_chars)
        paths = dict(changes.get("paths") or {})
        units: list[StateWriteUnit] = []
        notices: list[dict[str, Any]] = []
        if paths:
            provenance = {
                str(row.get("path")): row for row in (changes.get("wavetable") or [])
            }
            document = doc["document"] if isinstance(doc, dict) and "document" in doc else doc
            budget = _growth_budget(doc)
            pending: dict[str, Any] = {}
            table_units: list[tuple[str, dict[str, Any]]] = []
            sample_pair: list[StateWriteUnit] = []
            sample_pending: Any = None
            sample_growth = 0
            for path in paths:
                key = str(path)
                _check_root(key)
                _check_not_derived(key)
                note = self._check_wavetable_change(document, key, paths[path], provenance)
                if note is not None:
                    paths[key] = note.pop("normalized_value")
                    table_units.append((key, note))
                    continue
                limit = budget if key == SAMPLE_PATH else budget
                growth = _growth_of(document, key, paths[key])
                if key == SAMPLE_PATH and growth > limit:
                    # P39: the sample block is carried by OPENING ROOM for it, not dropped. Its
                    # growth is real (measured 99,508 / 132,300 characters on two presets) and a
                    # wavetable slot's balloon provides it; the room plan is built below, once
                    # the table units are known (the sample write has to follow its balloon
                    # immediately - see the ordering note there).
                    sample_pending = paths[key]
                    sample_growth = growth
                    continue
                if growth > limit:
                    # Measured (P38/P39): a write whose OWN growth exceeds the bound is
                    # silently IGNORED by the plugin - the document ends past the point the
                    # host hands over, and the value reads back unchanged. Attempting such a
                    # write can only fail verification and roll the transaction back, so the
                    # path is dropped HERE and named as a gap.
                    notices.append(
                        {
                            "reason": "growth_over_budget",
                            "path": key,
                            "growth_chars": growth,
                            "budget_chars": limit,
                            "message": (
                                "this value was NOT written: its growth exceeds the measured "
                                "landing bound of this channel (a write that grows the document "
                                "past it is silently ignored by the plugin on this build)"
                            ),
                        }
                    )
                    continue
                pending[key] = paths[key]
            # SCHEDULE EVERY PAYLOAD WRITE SO THE DOCUMENT'S PEAK STAYS LOWEST (P40).
            #
            # MEASURED (2026-10-18, live, three separate runs):
            #
            #   * a balloon's own write lands on the spot, and the plugin then holds the room it
            #     opened - so a pair's PEAK IS the document it leaves behind;
            #   * a write that SHRINKS the document is free room for everything after it (measured:
            #     the two shrinking tables of `BASS Broken Circuit` are what made room for its
            #     1.47 MB one, and withOUT them that balloon was refused by the read ceiling);
            #   * the SAMPLE pair must be scheduled like the others rather than pinned first. Pinned
            #     first it met the document while it still held the previous preset's 1.4 MB tables
            #     and was refused (`no_room_for_sample_growth`); the same preset frees ~1.5 MB a few
            #     units later, which is where the sample belongs.
            #
            # Both sizes of every item are known exactly (the live document and the target), so the
            # peaks below are arithmetic over measured numbers. Three candidate orders are simulated
            # and the one with the lowest peak wins; ties keep the first candidate, so the choice is
            # deterministic.
            items: list[dict[str, Any]] = []
            for key, note in table_units:
                growth = _table_growth(note)
                needs = bool(growth is not None and growth > budget)
                overhead = 0
                sizing: dict[str, Any] = {}
                if needs:
                    try:
                        sizing = wavetables_module.balloon_sizing(
                            paths[key],
                            dig(document, str(note.get("path"))),
                            budget_chars=budget,
                            stub=wavetables_module.frame_stub(paths[key]),
                        )
                    except InvalidValue:
                        sizing = {}
                    if not sizing.get("ok"):
                        notices.append(
                            {
                                "reason": "no_room_for_growth",
                                "path": note["path"],
                                "growth_chars": growth,
                                "budget_chars": budget,
                                "why": sizing.get("why"),
                                "message": (
                                    "this table was NOT written: it grows the document past "
                                    "the measured landing bound and no balloon can open "
                                    "enough room for it on this build"
                                ),
                            }
                        )
                        continue
                    overhead = max(
                        0,
                        int(sizing.get("room_chars") or 0) - int(sizing.get("replaced_chars") or 0),
                    )
                items.append(
                    {
                        "kind": "table",
                        "key": key,
                        "note": note,
                        "net": int(growth or 0),
                        "overhead": overhead,
                        "sizing": sizing,
                    }
                )
            if sample_pending is not None:
                sizing = self._sample_balloon_sizing(document, sample_pending)
                if sizing is None:
                    notices.append(
                        {
                            "reason": "no_room_for_sample_growth",
                            "path": SAMPLE_PATH,
                            "growth_chars": sample_growth,
                            "budget_chars": budget,
                            "message": (
                                "the sample payload was NOT written: it grows the document past "
                                "the measured landing bound, and the sample block could not be "
                                "inflated first (its `length` is beyond what this build holds, the "
                                "live block is too small for the balloon write itself to land, or "
                                "the result would pass the measured read ceiling)"
                            ),
                        }
                    )
                elif sizing["growth_chars"] > budget:
                    # the balloon's own write is a shrink of the live payload (measured), so this
                    # only happens on a live block with almost no payload to give up
                    notices.append(
                        {
                            "reason": "no_room_for_sample_growth",
                            "path": SAMPLE_PATH,
                            "growth_chars": sample_growth,
                            "budget_chars": budget,
                            "balloon_growth_chars": sizing["growth_chars"],
                            "message": (
                                "the sample payload was NOT written: the live sample block is too "
                                "small for its own balloon write to land in one step"
                            ),
                        }
                    )
                else:
                    items.append(
                        {
                            "kind": "sample",
                            "key": SAMPLE_PATH,
                            "value": sample_pending,
                            "net": int(sample_growth),
                            "overhead": max(
                                0, int(sizing["room_chars"]) - int(sizing["live_chars"])
                            ),
                            "sizing": sizing,
                        }
                    )

            order = _lowest_peak_order(items, _document_chars(document))
            running_chars = _document_chars(document)
            planned: list[StateWriteUnit] = []
            for item in order:
                projected = running_chars + int(item["overhead"])
                if projected > ceiling:
                    notices.append(
                        {
                            "reason": "state_ceiling_reached",
                            "path": (
                                SAMPLE_PATH if item["kind"] == "sample" else item["note"]["path"]
                            ),
                            "growth_chars": item["net"],
                            "projected_chars": projected,
                            "ceiling_chars": ceiling,
                            "other_track_chars": other_track_chars,
                            "document_chars_now": _document_chars(document),
                            "message": (
                                "this value was NOT written: delivering it would leave a state "
                                "whose track chunk passes what REAPER can read back (measured: "
                                "4,189,248 characters read fine, 4,240,000 faulted), and the read "
                                "is what every later step, including the rollback, needs. "
                                + (
                                    "The budget is the TRACK's and another plugin already "
                                    f"holds {other_track_chars} characters of it: free room "
                                    "there (remove or lighten the other plugin on this track), "
                                    "or load a smaller table or preset first"
                                    if other_track_chars
                                    else "Free room by loading a smaller table or preset first"
                                )
                            ),
                        }
                    )
                    continue
                if item["kind"] == "sample":
                    sizing = item["sizing"]
                    planned.append(self._sample_balloon_pair(sizing))
                    planned.append(
                        StateWriteUnit(
                            order_index=len(planned),
                            kind="state",
                            payload={"paths": {SAMPLE_PATH: item["value"]}},
                        )
                    )
                    notices.append(
                        {
                            "reason": "sample_inflated_before_write",
                            "path": SAMPLE_PATH,
                            "growth_chars": item["net"],
                            "balloon_length": sizing["length"],
                            "balloon_room_chars": sizing["room_chars"],
                            # MEASURED (2026-10-18, live): this notice says the value IS being
                            # written, in two steps - so it must not be read as "the sample did
                            # not arrive". Without the marker a fully successful load came back
                            # `loaded: false, partial: true` while both state rows read
                            # `agrees: true` (the write landed; only the flag was wrong).
                            "informational": True,
                            "message": (
                                "the sample block was written in two writes: the live block was "
                                "first re-declared with a longer `length` and a tiny payload (a "
                                "shrink, so it lands), which makes the plugin re-serialize a "
                                "bigger buffer than the file's - and the file's own payload then "
                                "lands as a shrink of that"
                            ),
                        }
                    )
                else:
                    note = item["note"]
                    if item["sizing"]:
                        balloon = self._plan_balloon(
                            document,
                            note,
                            paths[item["key"]],
                            budget,
                            document_chars=running_chars,
                            sizing=item["sizing"],
                            ceiling=ceiling,
                        )
                        if balloon is not None and "unit" in balloon:
                            note["balloon"] = balloon["sizing"]
                            note["balloon"]["unit"] = len(planned)
                            planned.append(balloon["unit"])
                    planned.append(
                        StateWriteUnit(
                            order_index=len(planned),
                            kind="state",
                            payload={"paths": {item["key"]: paths[item["key"]]}, "wavetable": [note]},
                        )
                    )
                running_chars += int(item["net"])
            for batch in _batch_paths(document, pending):
                units.append(
                    StateWriteUnit(order_index=len(units), kind="state", payload={"paths": batch})
                )
            # P40 ORDERING (measured): route ops stay first (P38's rule), then the scheduled items -
            # pairs keep their two writes adjacent - and the plain control batches last, where the
            # room the schedule made is already in the document.
            units = planned + units
        route_ops = (
            [{"op": "create", **spec} for spec in (changes.get("routes_create") or [])]
            + [{"op": "update", **spec} for spec in (changes.get("routes_update") or [])]
            + [{"op": "remove", **spec} for spec in (changes.get("routes_remove") or [])]
        )
        route_units: list[StateWriteUnit] = []
        for op in route_ops:
            route_units.append(
                StateWriteUnit(order_index=0, kind="state", payload={"route_op": op})
            )
        # P38 ordering: route operations ride BEFORE the plain path batches. A
        # create writes the slot's `modulation_N_*` fields from its own arguments
        # (power/stereo/bypass default to the measured neutral 0.0), so a preset
        # apply's control write for those same fields must come AFTER the route op
        # to land its file values - the reverse order would have the create
        # overwrite them with defaults.
        units = route_units + units
        for index, unit in enumerate(units):
            unit.order_index = index
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
            notices=notices,
        )

    def encode_slice(
        self, doc: dict[str, Any], unit: StateWriteUnit, current: TargetedStateSlice
    ) -> TargetedStateSlice:
        framing = parse_block_framing(current.block_text)
        document = copy.deepcopy(doc["document"] if isinstance(doc, dict) and "document" in doc else doc)
        payload = unit.payload or {}
        balloon = payload.get("balloon")
        if isinstance(balloon, dict):
            # The balloon is built HERE, from the live document: it inflates the table the
            # engine holds right now, with the stub payload the plan sized it with.
            slot_path = str(balloon.get("path") or "")
            table = wavetables_module.balloon_table(
                dig(document, slot_path),
                frames=int(balloon.get("frames") or 1),
                stub=balloon.get("stub"),
            )
            set_path(document, slot_path, table)
        sample_balloon = payload.get("sample_balloon")
        if isinstance(sample_balloon, dict):
            # The sample block is re-declared with a longer `length` and a few samples of the
            # TARGET (P39: the plugin sizes its buffer from `length`, so the read-back is bigger).
            block = dig(document, SAMPLE_PATH)
            if not isinstance(block, dict):
                raise InvalidValue(
                    "the live document carries no sample block to re-declare",
                    detail={"path": SAMPLE_PATH},
                )
            block["length"] = int(sample_balloon.get("length") or 0)
            written_payloads = sample_balloon.get("payload")
            written_payloads = written_payloads if isinstance(written_payloads, dict) else {}
            for key in SAMPLE_PAYLOAD_KEYS:
                if key in written_payloads:
                    block[key] = str(written_payloads[key])
                else:
                    block.pop(key, None)
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

    def _verify_balloon(self, document: Any, balloon: dict[str, Any]) -> VerificationReport:
        """Verify a balloon by its EFFECT: room for the table it is opening it for.

        The read-back is NOT the value that was written - the plugin re-serializes each stub
        keyframe as a FULL frame - so the balloon cannot be verified by equality (and that is
        the whole mechanism). What is verified is the quantity the next write needs: the
        read-back table's serialized size covers the table being delivered, and it carries at
        least the keyframes the balloon wrote.
        """
        slot_path = str(balloon.get("path") or "")
        current = dig(document, slot_path)
        frames_read_back = len(wavetables_module.frame_positions(current))
        room = wavetables_module.payload_bytes(current)
        wanted_frames = int(balloon.get("frames") or 0)
        target_chars = int(balloon.get("target_chars") or 0)
        ok = frames_read_back >= wanted_frames and room >= target_chars
        row = {
            "path": slot_path,
            "kind": "balloon",
            "frames_written": wanted_frames,
            "frames_read_back": frames_read_back,
            "target_chars": target_chars,
            "room_chars": room,
            "growth_chars": balloon.get("growth_chars"),
            "budget_chars": balloon.get("budget_chars"),
            "why": balloon.get("why"),
            "agrees": ok,
            "channel": "state",
        }
        return VerificationReport(
            ok=ok,
            rows=[row],
            mismatched=[] if ok else [row],
            comparators={"state.balloon": "room >= the table being delivered, read back"},
            tolerances={"state.float": 1e-05},
            space="state",
        )

    def verify(
        self, readback: dict[str, Any], unit: StateWriteUnit, policy: VerificationPolicy
    ) -> VerificationReport:
        tolerance = float(policy.tolerances.get("state.float", 1e-05))
        document = readback["document"] if isinstance(readback, dict) and "document" in readback else readback
        routes = {row["slot"]: row for row in list_routings(document, include_empty=True).values()}
        payload = unit.payload or {}
        if isinstance(payload.get("balloon"), dict) and not payload.get("paths"):
            return self._verify_balloon(document, payload["balloon"])
        if isinstance(payload.get("sample_balloon"), dict) and not payload.get("paths"):
            return self._verify_sample_balloon(document, payload["sample_balloon"])
        rows: list[dict[str, Any]] = []
        notes = {str(row.get("path")): row for row in (payload.get("wavetable") or [])}
        for path, value in (payload.get("paths") or {}).items():
            actual = dig(document, str(path))
            note = notes.get(str(path))
            if note is not None:
                # A wavetable row reports CONTENT IDENTITIES, not two multi-kilobyte blobs: the
                # outcome must stay readable, and the identity (a digest over the wave data) is
                # what a caller can compare. `agrees` is exact equality of the whole value with
                # the numeric tolerance applied field-wise - EXCEPT for an `Audio File Source`
                # payload, which the plugin cannot return byte-identical (measured requant band,
                # P39e) and which is therefore reported as its own comparison.
                differing, audio = _table_agreement(value, actual, tolerance)
                row: dict[str, Any] = {
                    "path": str(path),
                    "kind": "wavetable",
                    "oscillator": note.get("oscillator"),
                    "table_id_before": note.get("table_id_before"),
                    "table_id_written": note.get("table_id_after"),
                    "table_id_read_back": wavetables_module.table_id(actual)
                    if isinstance(actual, dict)
                    else None,
                    "n_frames": note.get("n_frames"),
                    "payload_bytes": note.get("payload_bytes"),
                    "rule": note.get("rule"),
                    "source": note.get("source"),
                    "stamped_version_to": note.get("stamped_version_to"),
                    "differing_fields": differing[:12],
                    "written": {
                        "table_id": note.get("table_id_after"),
                        "n_frames": note.get("n_frames"),
                        "payload_bytes": note.get("payload_bytes"),
                        "source_types": note.get("source_types"),
                    },
                    "read_back": wavetables_module.describe(actual),
                    "balloon": note.get("balloon"),
                    "agrees": not differing,
                    "channel": "state",
                }
                if audio is not None:
                    row["audio_payload"] = audio
                    row["read_back"]["table_id_note"] = (
                        "the id covers the audio payload as WRITTEN: the plugin's own requantization "
                        "moves up to a step of it (measured), which is what `audio_payload` reports"
                    )
                row["table_id_agrees"] = (
                    row["table_id_read_back"] is not None
                    and row["table_id_read_back"] == row["table_id_written"]
                )
                if not row["table_id_agrees"] and audio is not None and audio.get("within_band"):
                    # Honest reporting: for an audio-file slot the id CANNOT be expected to match.
                    # The id is a digest over the payload, and the plugin's int16 round trip moves
                    # up to a step of it (measured), so a landed write has a different id. What
                    # proves the write is the masked field comparison plus the band.
                    row["table_id_agrees_note"] = (
                        "expected to differ for an audio-file slot: the id is a digest of the "
                        "payload and the plugin's round trip moves up to 1 step of it, which "
                        "`audio_payload` measures; the write is verified by the field comparison "
                        "with the audio taken out, plus that band"
                    )
                rows.append(row)
                continue
            if str(path) == SAMPLE_PATH:
                # The plugin re-encodes the sample buffer on every state load; a
                # byte comparison of the base64 would report every landed write as
                # a failure. The row establishes what CAN be established (every
                # other field exact; the payload up to the measured reload delay)
                # and names the delay it measured.
                rows.append(_sample_row(str(path), value, actual, tolerance))
                continue
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

    def apply_intended_values(self, document: Any, values: Mapping[str, Any]) -> None:
        """Put values this transaction already wrote back into a document read from the plugin.

        The plugin re-encodes some payloads on every state load (measured: a sample's PCM comes back
        delayed by four samples), and every later unit re-sends the whole state - so without this,
        the file's own waveform drifts by one delay per following write (measured: 48 samples after a
        12-unit apply). Applying the INTENDED value keeps the instance on the target; the plugin's
        single round-trip delay stays, and it is the delay the verification band already reports.

        The caller hands over what `decode` returned, which WRAPS the document: a set on the wrapper
        would add a stray top-level key and leave the real payload drifting (measured - the carry
        silently did nothing until it unwrapped here, exactly as `encode_slice` and
        `snapshot_document` already do).
        """
        target = document["document"] if isinstance(document, dict) and "document" in document else document
        if not isinstance(target, dict):
            return
        for path, value in (values or {}).items():
            if not isinstance(path, str) or not path:
                continue
            try:
                set_path(target, path, copy.deepcopy(value))
            except (InvalidValue, Unreadable, PluginError):
                # a path the document no longer carries (a rollback removed it, or a slot shrank
                # away): the unit's own verification still guards what it writes now
                continue

    def snapshot_document(self, doc: dict[str, Any]) -> dict[str, Any]:
        """The restorable part: the whole document (V1 writes only touch it)."""
        document = doc["document"] if isinstance(doc, dict) and "document" in doc else doc
        return {"document": copy.deepcopy(document), "root": MUTABLE_ROOT}

    def restore_changes(self, doc: dict[str, Any], snap: dict[str, Any]) -> dict[str, Any]:
        """Restoration as document changes: every captured settings leaf, and every
        wavetable slot as a WHOLE table.

        Expressed as `paths` executed through `plan_state_writes`, never as a blob
        write (08 §3), so the rollback travels the same verified unit path as any
        other change.

        Leaves the plugin derives itself (`DERIVED_LEAVES`) are **excluded**: a
        write of a captured value for them cannot land (measured live), and a
        restore that demanded one would fail verification for a leaf that was
        never writable. The excluded paths are returned alongside the change set
        so Core can name them in the outcome instead of claiming more than it
        restored.

        **Wavetable slots are a second exception, and for the same reason shape-wise:**
        a slot's *leaves* (`...wavetables.0.groups.0.components.0.keyframes.0.wave_data`)
        are refused as write targets - a slot is written as a whole table, which is the
        shape the plugin's own channel accepts (measured) and the only shape the growth
        guard can bound. A slot is therefore restored as ONE path whose value is the
        captured table, and only when the live slot **differs** from the captured one
        (a rollback of an unrelated change must not rewrite three tables).
        """
        document = snap["document"]
        settings = document.get("settings") or {}
        leaves = flat_paths({MUTABLE_ROOT: settings})
        excluded = [path for path in DERIVED_LEAVES if path in leaves]
        for path in excluded:
            leaves.pop(path, None)
        live = doc if isinstance(doc, dict) else {}
        live_settings = live.get("settings") if isinstance(live.get("settings"), dict) else {}
        for path in [key for key in leaves if wavetables_module.parse_wavetable_path(key)]:
            leaves.pop(path, None)
        tables = settings.get(wavetables_module.SETTINGS_KEY)
        if isinstance(tables, list):
            for index, captured in enumerate(tables):
                slot_path = f"{MUTABLE_ROOT}.{wavetables_module.SETTINGS_KEY}.{index}"
                current = dig(live, slot_path)
                if current is None or current != captured:
                    leaves[slot_path] = captured
        # The top-level labels (P38: measured writable) are part of the document the
        # transaction wrote, so a rollback must cover them like any settings leaf.
        # Only a differing one is restored - a rollback of an unrelated change must
        # not rewrite metadata that already matches.
        for label in LABEL_PATHS:
            if label in document and live.get(label) != document[label]:
                leaves[label] = document[label]
        # The sample block: its payload leaf is plugin-derived (excluded above), but
        # a WHOLE-block restore is what makes a rollback of a preset apply complete.
        # The captured payload is pre-advanced by the measured reload delay, because
        # the plugin delays every written payload by exactly that much (measured:
        # without it the restore lands 4 samples late; with it, byte-exact).
        sample = settings.get(SAMPLE_BLOCK_KEY)
        if isinstance(sample, dict) and dig(live, SAMPLE_PATH) != sample:
            leaves[SAMPLE_PATH] = _sample_restore_value(sample)
        return {"paths": leaves, "derived_excluded": excluded}

    # -- internals ----------------------------------------------------------

    def _plan_balloon(
        self,
        document: Any,
        note: dict[str, Any],
        target: Any,
        budget: int,
        *,
        document_chars: int | None = None,
        sizing: dict[str, Any] | None = None,
        ceiling: int | None = None,
    ) -> dict[str, Any] | None:
        """Plan the write that opens room for a table too big to land on its own (P39).

        The mechanism is NOT "the bound grows": the bound follows the state's own length, so
        the room after the JSON is the same headroom for ever. It is that the balloon's
        RE-SERIALIZATION is bigger than the table being delivered - the plugin writes one full
        frame per stub keyframe (`WaveSourceKeyframe::jsonToState` memcpys a whole frame from a
        buffer sized for one) - so the table's own write becomes a SHRINK, and a shrink always
        lands (measured, 16 frames / 175,497 bytes; P39d measured the same for an `Audio File
        Source` slot: the stub replaces the audio source, the write drops ~1.4 MB and the
        read-back is `frames x 10,924` characters).

        The balloon writes into the SAME slot, so the table's write replaces it: nothing
        survives except the table, and the slot is one the transaction is already changing.
        While the balloon is live the slot renders the stub (a few seconds), which is why the
        stub carries the TARGET's own opening samples rather than noise.

        Returns ``{"unit", "sizing"}``, ``{"blocked": "state_ceiling", "projected_chars"}`` when the
        plan would land past what the host can read, or ``None`` when the live slot cannot afford
        enough stub keyframes - which the planner names as a gap rather than guessing a smaller one.
        """
        slot_path = str(note.get("path") or "")
        current = dig(document, slot_path)
        stub = wavetables_module.frame_stub(target)
        try:
            sizing = wavetables_module.balloon_sizing(
                target, current, budget_chars=budget, stub=stub
            )
        except InvalidValue:
            return None
        if not sizing.get("ok"):
            return None
        # The MEASURED read ceiling (2026-10-18): a state whose chunk text passes ~4 MB cannot be
        # read back at all, so the plan must not go there - the read is what every later step
        # (verification, rollback) needs. The projection is the document the balloon will MEET
        # (`document_chars`: the live size, plus the net effect of the units scheduled before it),
        # minus the slot the balloon rewrites, plus the room it will have afterwards.
        projected = (
            (document_chars if document_chars is not None else _document_chars(document))
            - max(0, int(sizing.get("replaced_chars") or 0))
            + int(sizing.get("room_chars") or 0)
        )
        allowed = _state_ceiling_chars() if ceiling is None else int(ceiling)
        if projected > allowed:
            return {
                "blocked": "state_ceiling",
                "projected_chars": projected,
                "ceiling_chars": allowed,
                "room_chars": int(sizing.get("room_chars") or 0),
            }
        unit = StateWriteUnit(
            order_index=0,
            kind="state",
            payload={
                "balloon": {
                    "path": slot_path,
                    "frames": sizing["frames"],
                    "stub": stub,
                    "target_chars": sizing["target_chars"],
                    "room_chars": sizing["room_chars"],
                    "growth_chars": _table_growth(note),
                    "write_growth_chars": sizing.get("write_growth_chars"),
                    "next_write_growth_chars": sizing.get("next_write_growth_chars"),
                    "budget_chars": budget,
                    "why": (
                        "a table whose write grows the document past the landing bound is "
                        "delivered in two writes: this one makes the state BIGGER than the "
                        "table (the plugin re-serializes one full frame per stub keyframe), so "
                        "the table's own write is a shrink - and a shrink always lands"
                    ),
                }
            },
        )
        return {"unit": unit, "sizing": sizing}

    def _check_wavetable_change(
        self,
        document: Any,
        path: str,
        value: Any,
        provenance: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Guard + normalize a write that addresses a wavetable slot.

        ``None`` when the path is not one. The guard lives here - not only in the
        semantic operation - because a caller can reach the same slot by writing the raw
        state path: a guard that only watched the operation would be bypassable (the same
        reasoning as the route-pair guard above). What it enforces, against the LIVE
        document (which request normalization never sees):

        1. the slot belongs to one of the three oscillators;
        2. the value is a WHOLE table carrying `Wave Source` frames (a partial write into
           `settings.wavetables.0.groups...` is not a way to select a table);
        3. the write does not GROW the payload (frames or serialized bytes) beyond what
           the engine holds - the measured host-abort boundary;
        4. `version` is stamped with the live plugin's own, because the plugin restamps it
           on re-serialisation and an unstamped value cannot be verified.
        """
        parsed = wavetables_module.parse_wavetable_path(path)
        if parsed is None:
            return None
        oscillator = parsed["oscillator"]
        if oscillator is None:
            raise InvalidValue(
                f"state path {path!r} addresses wavetable slot {parsed['index']}, which belongs "
                "to no oscillator: the document's table list is positional and this build has "
                f"three oscillators",
                detail={"path": path, "slots": list(wavetables_module.SLOTS)},
            )
        if parsed["suffix"]:
            raise InvalidValue(
                f"state path {path!r} writes *inside* a wavetable slot; a slot is written as a "
                "whole table (the plugin replaces the table it holds, it does not merge into it)",
                detail={"path": path, "slot_path": wavetables_module.path_for(oscillator)},
            )
        slot_path = wavetables_module.path_for(oscillator)
        tables = dig(document, f"{MUTABLE_ROOT}.{wavetables_module.SETTINGS_KEY}")
        if not isinstance(tables, list) or len(tables) <= parsed["index"]:
            raise InvalidValue(
                f"the live document does not address wavetable slot {parsed['index']}: the write "
                "would CREATE a slot instead of replacing one, and a slot the engine does not "
                "hold has no measured safe write",
                detail={
                    "path": slot_path,
                    "slot_index": parsed["index"],
                    "present": isinstance(tables, list),
                    "slots_in_document": len(tables) if isinstance(tables, list) else None,
                },
            )
        current = dig(document, slot_path)
        live_version = wavetables_module.live_version(document)
        verdict = wavetables_module.check_writable(
            value, current=current, live_version=live_version
        )
        stamped, stamp_note = wavetables_module.stamp_version(value, live_version)
        row = provenance.get(path) or provenance.get(slot_path) or {}
        note: dict[str, Any] = {
            "path": slot_path,
            "oscillator": oscillator,
            "table_id_before": wavetables_module.table_id(current) if isinstance(current, dict) else None,
            "table_id_after": verdict["table_id"],
            "n_frames": verdict["n_frames"],
            "payload_bytes": verdict["payload_bytes"],
            "source_types": verdict.get("source_types"),
            "current_n_frames": (verdict.get("current") or {}).get("n_frames"),
            "current_payload_bytes": (verdict.get("current") or {}).get("payload_bytes"),
            "current_source_types": (verdict.get("current") or {}).get("source_types"),
            "rule": verdict.get("rule"),
            "source": row.get("source"),
            "preset": row.get("preset"),
            "normalized_value": stamped,
        }
        note.update(stamp_note)
        return note

    def _sample_balloon_sizing(self, document: Any, target: Any) -> dict[str, Any] | None:
        """The room the SAMPLE block's own balloon would open - and what it costs, or ``None``.

        The plugin sizes its sample buffer from the JSON's ``length`` and re-serializes exactly that
        many samples (``Sample::jsonToState`` sizes a buffer from ``length`` and memcpys it), so a
        block written with a SHORT payload and a LONGER ``length`` reads back bigger than what was
        written - and the file's own payload then lands as a shrink of that. (Measured live, P39b.)

        What this returns is arithmetic over the live document and the target:
        ``length``/``room_chars`` (what the read-back will hold), ``live_chars`` (what the live block
        holds now), ``target_chars``, ``channels``, and the balloon's OWN ``growth_chars`` - which is
        a shrink on every preset measured, because the live payload it replaces is bigger than the
        few samples it writes. ``None`` means this build cannot inflate the block at all: no integer
        ``length``, no payload in the target, no live block, or a ``length`` the engine clamps.

        The ceiling is NOT judged here: the schedule decides when this pair runs, and therefore which
        document it meets (`_lowest_peak_order`).
        """
        if not isinstance(target, dict):
            return None
        target_length = target.get("length")
        payloads = {key: target.get(key) for key in SAMPLE_PAYLOAD_KEYS}
        payloads = {key: value for key, value in payloads.items() if isinstance(value, str) and value}
        if not isinstance(target_length, int) or target_length <= 0:
            return None
        if not payloads:
            return None
        live = dig(document, SAMPLE_PATH)
        if not isinstance(live, dict):
            return None
        #: The room the read-back must have: EVERY channel the target carries, because the plugin
        #: re-serializes each channel at `length` samples (8/3 characters per sample).
        target_chars = sum(len(value) for value in payloads.values())
        channels = len(payloads)
        want_chars = target_chars + HEADROOM_MARGIN_BYTES
        length = max(
            int(target_length * SAMPLE_BALLOON_MARGIN),
            math.ceil(want_chars * 3 / (8 * channels)),
        )
        length = max(length, target_length + 1)
        if length > SAMPLE_MAX_SAMPLES:
            return None
        balloon_block = copy.deepcopy(live)
        balloon_block["length"] = length
        #: A few samples of the TARGET, so the window is the file's own head, not noise, and the
        #: same CHANNEL COUNT, so the read-back has the channels the target will need.
        for key in SAMPLE_PAYLOAD_KEYS:
            if key in payloads:
                balloon_block[key] = _sample_head(payloads[key], samples=SAMPLE_BALLOON_SAMPLES)
            else:
                balloon_block.pop(key, None)
        growth = _growth_of(document, SAMPLE_PATH, balloon_block)
        room = (length * 8 * channels) // 3
        live_chars = sum(
            len(live[key]) for key in SAMPLE_PAYLOAD_KEYS if isinstance(live.get(key), str)
        )
        return {
            "length": length,
            "room_chars": room,
            "target_chars": target_chars,
            "channels": channels,
            "growth_chars": growth,
            "live_chars": live_chars,
            "target_length": target_length,
            "payload": {key: balloon_block[key] for key in payloads},
            "why": (
                "the file's sample payload grows the document past the landing bound, so the live "
                "block is first re-declared with a longer `length` and a tiny payload; the plugin "
                "then re-serializes that many samples and the file's payload lands as a shrink of it"
            ),
        }

    def _sample_balloon_pair(
        self, sizing: dict[str, Any]
    ) -> StateWriteUnit:
        """The unit that writes a sample balloon whose sizing is already decided."""
        return StateWriteUnit(
            order_index=0,
            kind="state",
            payload={
                "sample_balloon": {
                    "path": SAMPLE_PATH,
                    "length": sizing["length"],
                    "payload": sizing["payload"],
                    "target_length": sizing["target_length"],
                    "target_chars": sizing["target_chars"],
                    "channels": sizing["channels"],
                    "room_chars": sizing["room_chars"],
                    "growth_chars": sizing["growth_chars"],
                    "why": sizing["why"],
                }
            },
        )

    def _plan_sample_balloon(
        self, document: Any, target: Any, budget: int, *, ceiling: int | None = None
    ) -> tuple[StateWriteUnit, dict[str, Any]] | None:
        """Size the sample balloon AND judge it against the live document.

        Kept as the self-contained form of the pair (the schedule calls the two halves separately, so
        it can order the pair against the other writes): returns ``None`` when the block cannot be
        inflated, when the balloon's own write would not land, or when its projection passes the read
        ceiling on the LIVE document - the TRACK's ceiling when the caller knows it.
        """
        sizing = self._sample_balloon_sizing(document, target)
        if sizing is None or sizing["growth_chars"] > budget:
            return None
        projected = (
            _document_chars(document) - int(sizing["live_chars"]) + int(sizing["room_chars"])
        )
        allowed = _state_ceiling_chars() if ceiling is None else int(ceiling)
        if projected > allowed:
            return None
        return self._sample_balloon_pair(sizing), sizing

    def _verify_sample_balloon(self, document: Any, balloon: dict[str, Any]) -> VerificationReport:
        """Verify a sample balloon by its EFFECT: the read-back block is the room it promised.

        The room is EVERY channel's payload, because the plugin re-serializes each channel at
        ``length`` samples (`Sample::stateToJson` writes `samples_stereo` for a stereo buffer).
        Measuring only `samples` under-reports it by half - which is how a stereo preset's write
        was refused while the room it needed was actually there (measured live).
        """
        live = dig(document, SAMPLE_PATH)
        length_read = live.get("length") if isinstance(live, dict) else None
        channels = {
            key: len(live[key]) for key in SAMPLE_PAYLOAD_KEYS
            if isinstance(live, dict) and isinstance(live.get(key), str)
        }
        chars = sum(channels.values())
        wanted = int(balloon.get("length") or 0)
        target_chars = int(balloon.get("target_chars") or 0)
        ok = length_read == wanted and chars >= target_chars
        row = {
            "path": SAMPLE_PATH,
            "kind": "sample_balloon",
            "length_written": wanted,
            "length_read_back": length_read,
            "target_chars": target_chars,
            "room_chars": chars,
            "room_by_channel": channels,
            "growth_chars": balloon.get("growth_chars"),
            "budget_chars": balloon.get("budget_chars"),
            "why": balloon.get("why"),
            "agrees": ok,
            "channel": "state",
        }
        return VerificationReport(
            ok=ok,
            rows=[row],
            mismatched=[] if ok else [row],
            comparators={"state.sample_balloon": "read-back lengthened to `length`, payload >= the file's"},
            tolerances={"state.float": 1e-05},
            space="state",
        )

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
            named = route_op.get("slot")
            if named is None:
                slot = find_free_slot(document)
                if slot is None:
                    raise InvalidValue(
                        "no free modulation slot", detail={"slots": ROUTE_SLOTS, "reason": "no_free_slot"}
                    )
            else:
                # P38: a caller may name the slot (a preset apply reproduces the
                # FILE's own slot numbering, because the per-slot
                # `modulation_N_*` amounts live at that index). A named slot must
                # be FREE: creating into an occupied one is an update, and
                # silently landing elsewhere is how a file's routing would pair
                # with another slot's amount.
                settings_for_slot = _settings(document)
                mods = _modulations(settings_for_slot)
                slot = int(named)
                if not 1 <= slot <= len(mods):
                    raise InvalidValue(
                        f"route slot {slot} is outside 1..{len(mods)}",
                        detail={"slot": slot, "slots": len(mods)},
                    )
                current = mods[slot - 1] if isinstance(mods[slot - 1], dict) else {}
                if str(current.get("source") or "").strip() or str(current.get("destination") or "").strip():
                    raise InvalidValue(
                        f"route slot {slot} is occupied; a create names a FREE slot (or none, and the "
                        "first free slot is used)",
                        detail={"slot": slot, "reason": "slot_occupied"},
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


def _headroom_bytes(B: bytes, brace: int) -> int | None:
    """The room a state write has left after the document, in characters.

    ``len(item B) - HOST_TRAILER_BYTES - (the end of the JSON)``: the measured landing rule
    (P39) says a write lands iff the NEW document ends at or before ``len(item B) - 8``, so a
    write may grow the document by at most this. ``None`` when the document's end cannot be
    located, which is not the same as "no room" and must not be treated as one.
    """
    decoded = _decode_at(B, brace)
    if decoded is None:
        return None
    _obj, consumed = decoded
    return len(B) - HOST_TRAILER_BYTES - (brace + consumed)


def _framing_summary(framing: dict[str, Any], *, headroom: int | None = None) -> dict[str, Any]:
    """What may travel in a DocumentView: sizes and widths, never the bytes."""
    out = {
        "item_a_bytes": len(framing["A"]),
        "item_b_bytes": len(framing["B"]),
        "b64_width": framing.get("b64_width"),
        "b64_artifact_len": len(framing.get("b64_artifact") or ""),
    }
    if headroom is not None:
        out["headroom_bytes"] = headroom
        out["headroom_source"] = "measured: len(item B) - 8 - the document's end (P39)"
    return out


def _agrees(written: Any, read_back: Any, tolerance: float) -> bool:
    if isinstance(written, bool) or isinstance(read_back, bool):
        return written == read_back
    if isinstance(written, (int, float)) and isinstance(read_back, (int, float)):
        return abs(float(written) - float(read_back)) <= tolerance
    return written == read_back


# ---------------------------------------------------------------------------
# Growth-budget batching and the sample block (P38, measured 2026-10-16/17)
# ---------------------------------------------------------------------------


def _serialized_len(value: Any) -> int:
    """A value's size in the document's own JSON encoding (`encode_item_b`'s)."""
    try:
        return len(json.dumps(value, separators=(",", ":")))
    except (TypeError, ValueError):  # pragma: no cover - unencodable value
        return 0


def _state_ceiling_chars(other_track_chars: int = 0) -> int:
    """The document size whose chunk text still stays inside the measured read limit.

    A plan that lands past this leaves an instance the layer can no longer READ - and the read is
    the first step of every operation, including the rollback. Measured, not estimated: see
    `STATE_CHUNK_CEILING_CHARS`.

    MEASURED (2026-10-18, live) why `other_track_chars` comes off first: the ceiling is a limit on
    the TRACK's chunk, because that is what `GetTrackStateChunk` returns. Two Vital instances on one
    track - each one inside this per-instance budget - put the track's chunk past it, the host
    faulted on every read of that track, and the instance could be neither read nor restored
    (`ROLLBACK_FAILED`, `write_effects_unknown: 1`). The count is the chunk text that does not
    belong to this instance's own block, so a track shared with another plugin is budgeted for both.
    """
    allowed = STATE_CHUNK_CEILING_CHARS - STATE_CEILING_MARGIN_CHARS - max(0, int(other_track_chars))
    return max(0, int(allowed / STATE_CHUNK_EXPANSION))


def _document_chars(document: Any) -> int:
    """The serialized size of the whole document (what the chunk text scales from)."""
    return max(0, payload_size(document))


def payload_size(value: Any) -> int:
    """`len(json.dumps(value, separators))` without materializing a 3 MB string twice."""
    try:
        return len(json.dumps(value, separators=(",", ":"), default=str))
    except Exception:  # noqa: BLE001 - an unencodable document is not measurable
        return 0


def _growth_of(document: Any, path: str, value: Any) -> int:
    """The serialized growth a single path change is expected to add.

    The estimate is the same one every measured batch was packed with; it was
    observed to undershoot the real encoded delta by at most ~12 chars, which the
    conservative `BATCH_GROWTH_BUDGET` absorbs.
    """
    current = dig(document, path)
    return _serialized_len(value) - (_serialized_len(current) if current is not None else 0)


def _growth_budget(doc: Any) -> int:
    """The growth ONE write may add, from the LIVE framing when it is known.

    `decode()` reports the exact headroom (`len(item B) - 8 - the end of the document`, P39);
    the constant is the offline fallback, and `HEADROOM_MARGIN_BYTES` is what keeps an
    estimate that undershoots by a few characters inside the measured bound.
    """
    headroom: int | None = None
    if isinstance(doc, dict):
        framing = doc.get("framing")
        if isinstance(framing, dict):
            candidate = framing.get("headroom_bytes")
            if isinstance(candidate, int) and candidate > 0:
                headroom = candidate
    if headroom is None:
        headroom = STATE_HEADROOM_BYTES
    return max(0, headroom - HEADROOM_MARGIN_BYTES)


def _table_growth(note: dict[str, Any]) -> int | None:
    """The document growth a wavetable note's write produces, when both sizes are known.

    ``None`` - never 0 - when either size is unavailable: "unknown" must not read as "fits".
    """
    after = note.get("payload_bytes")
    before = note.get("current_payload_bytes")
    if not isinstance(after, int) or after < 0:
        return None
    if not isinstance(before, int) or before < 0:
        return None
    return after - before


def _running_document_chars(document: Any, sample_pending: Any, sample_growth: int) -> int:
    """The document size the TABLE schedule will start from.

    Kept for callers that need the historical "sample first" number; the schedule itself simulates
    each candidate order from the live size (`_lowest_peak_order`), so it no longer needs this.
    """
    size = _document_chars(document)
    if sample_pending is not None and sample_growth > 0:
        size += int(sample_growth)
    return size


def _lowest_peak_order(items: Sequence[dict[str, Any]], start_chars: int) -> list[dict[str, Any]]:
    """The order in which these writes reach the LOWEST document peak (P40).

    Every item carries two numbers, both exact (the live document and the target are known):

    * ``net`` - what the document gains once the item is done (negative = it shrinks it);
    * ``overhead`` - what the document holds WHILE the item runs, above its own starting size. A
      balloon's transient is exactly this: the room it opens minus the content it replaces. A plain
      write's overhead is zero.

    So an item's peak is ``running + overhead``, and the order decides every peak after the first.
    Three candidate orders are simulated and the one with the lowest peak wins (ties keep the first,
    so the choice is deterministic): biggest shrink first (by net), cheapest overhead first, and
    dearest overhead first.

    MEASURED (2026-10-18, live) why this is not academic: the SAMPLE pair pinned first met the
    document while it still held the previous preset's 1.4 MB tables and was refused, while the same
    preset frees 1.5 MB two units later; and with the tables of `BASS Broken Circuit` ordered by
    net, the document never rises above what it started at.
    """
    usable = [item for item in items if isinstance(item, dict)]
    if not usable:
        return []

    def peak_for(order: Sequence[dict[str, Any]]) -> int:
        running = int(start_chars)
        peak = running
        for item in order:
            peak = max(peak, running + int(item.get("overhead") or 0))
            running += int(item.get("net") or 0)
        return peak

    candidates = [
        sorted(usable, key=lambda item: int(item.get("net") or 0)),
        sorted(usable, key=lambda item: int(item.get("overhead") or 0)),
        sorted(usable, key=lambda item: -int(item.get("overhead") or 0)),
    ]
    best = candidates[0]
    best_peak = peak_for(best)
    for candidate in candidates[1:]:
        peak = peak_for(candidate)
        if peak < best_peak:
            best, best_peak = candidate, peak
    return list(best)


def _batch_paths(document: Any, pending: dict[str, Any]) -> list[dict[str, Any]]:
    """Greedy-pack path changes so each batch stays inside the growth budget.

    A batch is at least one path: the codec never splits a single change, and a path
    whose own estimate exceeds `SINGLE_PATH_GROWTH_LIMIT` never reaches this packer
    (it is named as a gap instead), so an over-budget batch cannot be built here.
    """
    batches: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    cost = 0
    for key, value in pending.items():
        item = _growth_of(document, key, value)
        if current and (len(current) >= BATCH_MAX_PATHS or cost + item > BATCH_GROWTH_BUDGET):
            batches.append(current)
            current = {}
            cost = 0
        current[key] = value
        cost += item
    if current:
        batches.append(current)
    return batches


def _sample_head(payload: str, *, samples: int) -> str:
    wanted = 2 * max(1, int(samples))
    head = payload[: 4 * ((wanted + 2) // 3)]
    try:
        raw = base64.b64decode(head + "=" * (-len(head) % 4))
    except Exception:  # noqa: BLE001 - a malformed payload starts the balloon on silence
        raw = b""
    return base64.b64encode((raw + b"\x00" * wanted)[:wanted]).decode("ascii")


def _sample_restore_value(sample: dict[str, Any], delay: int = SAMPLE_RELOAD_DELAY) -> dict[str, Any]:
    """The captured sample block with its payload pre-advanced by the reload delay.

    The plugin delays every written payload by exactly `SAMPLE_RELOAD_DELAY`
    samples on the state load (measured repeatedly), so a restore that writes the
    captured value verbatim would land 4 samples late. Pre-advancing inverts the
    measured delay; if the delay ever differed, verification would see the shift
    it actually got and refuse the restore instead of hiding it.
    """
    out = copy.deepcopy(sample)
    payload = out.get("samples")
    if isinstance(payload, str) and payload:
        try:
            raw = b64d(payload)
            shift = 2 * int(delay)
            if 0 < shift < len(raw):
                out["samples"] = _b64e(raw[shift:] + b"\x00" * shift)
        except Exception:  # noqa: BLE001 - an undecodable payload restores as-is
            pass
    return out


def _sample_alignment(written: Any, read_back: Any) -> dict[str, Any] | None:
    """Compare two base64 int16 payloads up to the measured reload delay.

    Returns the best alignment's shift and residual; ``within_delay`` is the
    verdict the caller may rely on. ``None`` when a payload is not comparable
    (wrong type or undecodable) - never a silent "fine".
    """
    if not isinstance(written, str) or not isinstance(read_back, str):
        return None
    if written == read_back:
        return {
            "shift": 0,
            "n_samples": None,
            "residual_differing": 0,
            "residual_max_abs": 0,
            "within_delay": True,
            "exact": True,
        }
    try:
        left = array.array("h")
        left.frombytes(b64d(written))
        right = array.array("h")
        right.frombytes(b64d(read_back))
    except Exception:  # noqa: BLE001 - undecodable payload is not a comparison
        return None
    if len(left) != len(right):
        return {
            "shift": None,
            "n_samples": [len(left), len(right)],
            "within_delay": False,
            "reason": "payload_length_mismatch",
        }
    search = SAMPLE_RELOAD_DELAY + 4
    best: tuple[int, int, int] | None = None
    for shift in range(-search, search + 1):
        a = left[max(0, shift):]
        b = right[max(0, -shift):]
        n = min(len(a), len(b))
        err = sum(1 for index in range(0, n, 7) if a[index] != b[index])
        if best is None or err < best[1]:
            best = (shift, err, n)
    shift, sampled_errors, _n = best
    a = left[max(0, shift):]
    b = right[max(0, -shift):]
    span = min(len(a), len(b))
    residual = 0
    max_abs = 0
    for index in range(span):
        if a[index] != b[index]:
            residual += 1
            delta = abs(b[index] - a[index])
            if delta > max_abs:
                max_abs = delta
    within = residual <= max(64, int(SAMPLE_REQUANT_BAND * span)) and max_abs <= SAMPLE_REQUANT_STEP
    return {
        "shift": shift,
        "n_samples": span,
        "sampled_mismatches": sampled_errors,
        "residual_differing": residual,
        "residual_max_abs": max_abs,
        "requant_band": SAMPLE_REQUANT_BAND,
        "requant_step": SAMPLE_REQUANT_STEP,
        "within_delay": within,
    }


def _audio_payload_band(written: Any, read_back: Any) -> dict[str, Any]:
    """Compare two ``audio_file`` payloads by what the plugin can preserve (measured band).

    MEASURED (P39e, 2026-10-17, live 1.5.5, twice on a 1,409,808-byte growl table): the plugin
    converts int16 -> float on load and float -> int16 on save, so 8,856 of 528,388 samples
    (1.676 %) come back moved by EXACTLY one step (histogram: `{1: 8856}`, identical in both
    trials). A byte comparison would therefore report a landed write as a failure, and a
    looser one would hide a wrong table. The verdict is: same LENGTH always, and every moved
    sample within `AUDIO_REQUANT_STEP`.
    """
    if not isinstance(written, str) or not isinstance(read_back, str):
        return {
            "n_samples": None,
            "within_band": False,
            "reason": "payload_missing" if not isinstance(read_back, str) else "written_missing",
        }
    if written == read_back:
        return {
            "n_samples": len(written) // 4,
            "differing": 0,
            "differing_fraction": 0.0,
            "max_abs_delta": 0,
            "step": wavetables_module.AUDIO_REQUANT_STEP,
            "within_band": True,
            "exact": True,
        }
    try:
        left = array.array("h")
        left.frombytes(base64.b64decode(written + "=" * (-len(written) % 4)))
        right = array.array("h")
        right.frombytes(base64.b64decode(read_back + "=" * (-len(read_back) % 4)))
    except Exception:  # noqa: BLE001 - an undecodable payload is not a comparison
        return {"n_samples": None, "within_band": False, "reason": "payload_undecodable"}
    if len(left) != len(right):
        return {
            "n_samples": [len(left), len(right)],
            "within_band": False,
            "reason": "payload_length_mismatch",
        }
    differing = 0
    worst = 0
    for index, (one, other) in enumerate(zip(left, right)):
        if one != other:
            differing += 1
            worst = max(worst, abs(int(one) - int(other)))
    return {
        "n_samples": len(left),
        "differing": differing,
        "differing_fraction": round(differing / max(1, len(left)), 6),
        "max_abs_delta": worst,
        "step": wavetables_module.AUDIO_REQUANT_STEP,
        "within_band": worst <= wavetables_module.AUDIO_REQUANT_STEP,
        "exact": differing == 0,
    }


def _table_agreement(written: Any, read_back: Any, tolerance: float) -> tuple[list[str], dict[str, Any] | None]:
    """Field-wise comparison of two tables, with the AUDIO payload taken out and banded.

    An `Audio File Source` slot (P39: how the growl/dubstep family stores its table) carries
    one multi-hundred-kilobyte payload that the plugin cannot return byte-identical, so it is
    compared separately and everything else - the component graph, the keyframe positions, the
    window and rate fields, the sample layer - stays exact.
    """
    written_audio = wavetables_module.audio_payload(written) if isinstance(written, dict) else None
    read_audio = wavetables_module.audio_payload(read_back) if isinstance(read_back, dict) else None
    if written_audio is None and read_audio is None:
        return _differing_fields(written, read_back, tolerance), None
    masked_written = _without_audio(written)
    masked_read = _without_audio(read_back)
    differing = _differing_fields(masked_written, masked_read, tolerance)
    band = _audio_payload_band(written_audio, read_audio)
    if not band.get("within_band"):
        differing.append("audio_file (outside the measured requantization band)")
    return differing, band


def _without_audio(table: Any) -> Any:
    """The table with every ``audio_file`` payload replaced by a marker (no 1.4 MB copies)."""
    if not isinstance(table, dict):
        return table
    stripped = copy.deepcopy(table)
    for group in stripped.get("groups") or []:
        if not isinstance(group, dict):
            continue
        for comp in group.get("components") or []:
            if isinstance(comp, dict) and isinstance(comp.get("audio_file"), str):
                comp["audio_file"] = "<compared separately>"
    return stripped


def _sample_row(path: str, written: Any, read_back: Any, tolerance: float) -> dict[str, Any]:
    """The verification row for a sample block (never embeds the payloads).

    EVERY payload key is taken out of the field comparison and compared through the measured
    alignment/requant band - not just `samples`: a preset's sample may be STEREO, and then
    `samples_stereo` is a second payload the plugin re-encodes just the same (measured on a
    family preset whose stereo sample refused the write when its second channel was compared as
    a plain field).
    """
    written = written if isinstance(written, dict) else {}
    read_back = read_back if isinstance(read_back, dict) else {}
    written_fields = {k: v for k, v in written.items() if k not in SAMPLE_PAYLOAD_KEYS}
    read_fields = {k: v for k, v in read_back.items() if k not in SAMPLE_PAYLOAD_KEYS}
    differing = _differing_fields(written_fields, read_fields, tolerance)
    payloads: dict[str, Any] = {}
    for key in SAMPLE_PAYLOAD_KEYS:
        if not isinstance(written.get(key), str) and not isinstance(read_back.get(key), str):
            continue
        payloads[key] = _sample_alignment(written.get(key), read_back.get(key))
    agrees = (
        not differing
        and bool(payloads)
        and all(row is not None and row.get("within_delay") for row in payloads.values())
    )
    row: dict[str, Any] = {
        "path": path,
        "kind": "sample",
        "written_fields": written_fields,
        "read_back_fields": read_fields,
        "differing_fields": differing[:8],
        "payload_written_chars": {
            key: len(written[key]) for key in SAMPLE_PAYLOAD_KEYS if isinstance(written.get(key), str)
        },
        "payload_read_back_chars": {
            key: len(read_back[key]) for key in SAMPLE_PAYLOAD_KEYS if isinstance(read_back.get(key), str)
        },
        "payloads": payloads,
        "agrees": agrees,
        "channel": "state",
    }
    #: the single-payload shape is kept for the rows written before a second channel existed
    first = payloads.get("samples")
    row["payload_compared"] = first
    if agrees and first is not None and not first.get("exact"):
        row["unverifiable"] = {
            "leaf": f"{path}.samples",
            "reason": (
                "the plugin re-encodes the sample buffer on every state load: the read-back "
                f"payload equals the written one delayed by {first.get('shift')} sample(s) "
                f"(measured reload delay {SAMPLE_RELOAD_DELAY}), with "
                f"{first.get('residual_differing')} of {first.get('n_samples')} samples "
                f"moved by at most {first.get('residual_max_abs')} step(s) - the plugin's own "
                "truncating int16 requantization (measured). Byte equality is not obtainable on "
                "this channel even though the write landed; this row states what was measured."
            ),
        }
    return row


def _differing_fields(written: Any, read_back: Any, tolerance: float, prefix: str = "") -> list[str]:
    """The dotted fields of a structured value that do not agree (bounded by callers).

    A whole-value ``==`` is the first test (that is what the pointer-based comparison
    does, and it is O(1) when the values agree). Only a difference walks the structure,
    so a *reported* difference names the field instead of dumping two 11 KB trees - the
    POC's measured lesson was that one cosmetic field (`version`) made an exact
    comparison report a landed write as a failure, and a row that had said **which**
    field differed would have shown that in one line.
    """
    if isinstance(written, dict) and isinstance(read_back, dict):
        out: list[str] = []
        for key in sorted(set(written) | set(read_back)):
            name = f"{prefix}.{key}" if prefix else str(key)
            if key not in written:
                out.append(f"{name} (absent in written)")
            elif key not in read_back:
                out.append(f"{name} (absent in read-back)")
            else:
                out.extend(_differing_fields(written[key], read_back[key], tolerance, name))
        return out
    if isinstance(written, list) and isinstance(read_back, list):
        out = []
        if len(written) != len(read_back):
            return [f"{prefix} (length {len(written)} vs {len(read_back)})"]
        for index, (left, right) in enumerate(zip(written, read_back)):
            out.extend(_differing_fields(left, right, tolerance, f"{prefix}.{index}"))
        return out
    return [] if _agrees(written, read_back, tolerance) else [prefix or "<value>"]
