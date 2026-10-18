"""Codec B: a second state codec with a DIFFERENT shape and batching.

Its document is a nested key/value tree serialized as plain ``path=json``
lines (no base64, no JSON envelope), and its planner splits a logical change by
top-level section, so a change that touches two sections is TWO ordered state
units - while codec A would write the same change in ONE unit. The codec
conformance suite runs both, so no engine test can depend on one provider's
document shape or batching (AUD-004).
"""

from __future__ import annotations

import copy
import json
from typing import Any

import fixtures as F

from reaper_reapy_mcp.plugins.errors import InvalidValue
from reaper_reapy_mcp.plugins.model import (
    StateWritePlan,
    StateWriteUnit,
    TargetedStateSlice,
    VerificationPolicy,
    VerificationReport,
)

B_HEADER = (
    '<VST "VST3b: Demo B" demo_b.vst3 0 "" FFEEDDCCBBAA99887766554433221100 '
    "0 2 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0>"
)
B_END = "#end"


def b_document() -> dict[str, Any]:
    return {
        "tree": {"engine": {"mode": "a", "gain": 0.3}, "output": {"level": 0.5}},
        "flags": {"enabled": True, "bypass": False},
    }


def make_b_block_text(flat: dict[str, Any], *, header: str = B_HEADER) -> str:
    lines = [header]
    for path in sorted(flat):
        lines.append(f"{path}={json.dumps(flat[path])}")
    lines.append(B_END)
    lines.append(">")
    return "\n".join(lines)


def parse_b_payload(block_text: str) -> dict[str, Any]:
    lines = block_text.split("\n")
    flat: dict[str, Any] = {}
    for raw_line in lines[1:-1]:
        line = raw_line.rstrip("\r")
        if line == B_END or not line:
            continue
        path, sep, raw_value = line.partition("=")
        if not sep:
            raise InvalidValue(f"codec B payload line has no '=': {line!r}")
        flat[path] = json.loads(raw_value)
    return flat


def nest(flat: dict[str, Any]) -> dict[str, Any]:
    """Flat dotted paths -> nested dict (codec B's document shape)."""
    doc: dict[str, Any] = {}
    for path in sorted(flat):
        tokens = path.split(".")
        node = doc
        for token in tokens[:-1]:
            node = node.setdefault(token, {})
        node[tokens[-1]] = flat[path]
    return doc


class CodecB:
    name = "tree-lines-v1"

    # -- document <-> block -------------------------------------------------

    def decode(self, slice: TargetedStateSlice) -> dict[str, Any]:
        return nest(parse_b_payload(slice.block_text))

    def encode_slice(
        self, doc: dict[str, Any], unit: StateWriteUnit, current: TargetedStateSlice
    ) -> TargetedStateSlice:
        flat = F.flat_paths(doc)
        for path, value in ((unit.payload or {}).get("paths") or {}).items():
            if "." not in path:
                raise InvalidValue(
                    f"codec B paths must be section-rooted: {path!r}", detail={"path": path}
                )
            flat[path] = value
        return TargetedStateSlice(
            chunk_digest=current.chunk_digest,
            line_start=current.line_start,
            line_end=current.line_end,
            header_line=current.header_line,
            class_hex=current.class_hex,
            ordinal=current.ordinal,
            block_text=make_b_block_text(flat, header=current.header_line),
            framing=None,
        )

    def plan_state_writes(
        self, doc: dict[str, Any], changes: dict[str, Any], *, other_track_chars: int = 0
    ) -> StateWritePlan:
        paths = dict(changes.get("paths") or {})
        sections = sorted({path.split(".", 1)[0] for path in paths})
        units: list[StateWriteUnit] = []
        for order, section in enumerate(sections):
            section_paths = {
                path: value for path, value in paths.items() if path.split(".", 1)[0] == section
            }
            units.append(
                StateWriteUnit(
                    order_index=order, kind="state", payload={"paths": section_paths, "section": section}
                )
            )
        return StateWritePlan(units=units, verification_plan={"space": "native", "by_section": True})

    def verify(
        self, readback: dict[str, Any], unit: StateWriteUnit, policy: VerificationPolicy
    ) -> VerificationReport:
        tolerance = float(policy.tolerances.get("state.float", 1e-05))
        flat = F.flat_paths(readback)
        rows: list[dict[str, Any]] = []
        for path, value in ((unit.payload or {}).get("paths") or {}).items():
            actual = flat.get(path)
            agree = value == actual
            if isinstance(value, (int, float)) and isinstance(actual, (int, float)) and not isinstance(value, bool):
                agree = abs(float(value) - float(actual)) <= tolerance
            rows.append(
                {"path": path, "written": value, "read_back": actual, "agrees": agree, "channel": "state"}
            )
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
        return {"document": copy.deepcopy(doc), "routes": []}

    def restore_changes(self, doc: dict[str, Any], snap: dict[str, Any]) -> dict[str, Any]:
        return {"paths": F.flat_paths(snap["document"])}
