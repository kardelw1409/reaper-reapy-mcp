"""Fixture factories and byte utilities for the offline plugin suite.

Everything here is deliberately generic: no real-provider identifier appears in
these core fixtures (08 §8.5, L0). The block format below is the *mock* codec's
own format; the host-level chunk envelope (`<VST ...>` header line, payload
lines, lone `>` terminator) mimics the host wire format so span location and
byte-exact splicing are exercised the way they are in reality.

Utilities:
- block/chunk builders (single, foreign, duplicated blocks; CRLF variant)
- ``split_outside`` / ``byte_diff_span`` for "untouched blocks byte-identical"
- a valid demo profile + facts + parameter vector for the mock provider/host
"""

from __future__ import annotations

import base64
import copy
import json
from typing import Any, Iterable, Sequence

DEMO_PROVIDER = "demo"
DEMO_NAME = "VST3i: Demo Synth"
DEMO_MODULE = "demo.vst3"
DEMO_CLASS = "AABBCCDDEEFF00112233445566778899"

FOREIGN_NAME = "VST3i: Other Synth"
FOREIGN_MODULE = "other.vst3"
FOREIGN_CLASS = "11112222333344445555666677778888"

B64_WIDTH = 120

#: The mock codec's parameter table: (ident, initial normalized value).
DEMO_PARAMS: tuple[tuple[str, float], ...] = (
    ("0:1000", 0.10),
    ("1:1001", 0.20),
    ("2:1002", 0.30),
    ("3:1003", 0.50),
    ("4:1004", 0.50),
    ("5:1005", 0.00),
    ("6:1006", 0.25),
    ("7:1007", 0.75),
)

#: The **names the host reports** for those parameters, by index. They are the
#: live-checked half of a control binding (P8): a control is only writable when
#: the host's name at the declared index equals one of the names the profile
#: declares for it, so a host double without names could not exercise the rule
#: at all. Indices 3/4/5/6 match the demo profile's declared labels.
DEMO_PARAM_NAMES: tuple[str, ...] = (
    "Channel 0 Level",
    "Channel 1 Level",
    "Channel 2 Level",
    "Tone Cutoff",
    "Tone Level",
    "Tone Shape",
    "Mix %",
    "Output",
)

#: Serialized parameter value space used by the state codec (documents store
#: only state-channel fields; control values live in the parameter array).
DEMO_DOCUMENT_TEMPLATE: dict[str, Any] = {
    "version": 1,
    "settings": {"mode": "poly", "hq": True},
    "routes": [
        {"slot": 1, "source": "src_a", "destination": "tone_cutoff", "amount": 0.25, "bipolar": False},
        {"slot": 2, "source": "src_b", "destination": "tone_level", "amount": 0.0, "bipolar": False},
    ],
    "meta": {"author": "demo", "schema": 1},
}

ROUTE_SLOTS = 8


def demo_document() -> dict[str, Any]:
    return copy.deepcopy(DEMO_DOCUMENT_TEMPLATE)


def demo_params() -> dict[str, float]:
    return {ident: value for ident, value in DEMO_PARAMS}


def demo_facts(
    track_index: int = 0,
    fx_index: int = 0,
    *,
    name: str = DEMO_NAME,
    module: str = DEMO_MODULE,
    class_id: str = DEMO_CLASS,
    param_count: int = len(DEMO_PARAMS),
    version: str = "1.0.0",
) -> dict[str, Any]:
    """FxHostFacts-shaped dict (the mock host / tests convert as needed)."""
    return {
        "track_index": track_index,
        "fx_index": fx_index,
        "fx_name": name,
        "class_id": class_id,
        "module": module,
        "param_count": param_count,
        "version": version,
        "track_name": "Track 1",
        "fx_count": 1,
        "session_handle": None,
    }


# ---------------------------------------------------------------------------
# Mock codec block format ("b64-json-block-v1")
# ---------------------------------------------------------------------------


def wrap_text(text: str, width: int = B64_WIDTH) -> list[str]:
    return [text[i : i + width] for i in range(0, len(text), width)]


def make_block_header(
    *,
    name: str = DEMO_NAME,
    module: str = DEMO_MODULE,
    class_hex: str = DEMO_CLASS,
) -> str:
    return f'<VST "{name}" {module} 0 "" {class_hex} 0 2 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0>'


def make_block_text(
    doc: dict[str, Any],
    *,
    name: str = DEMO_NAME,
    module: str = DEMO_MODULE,
    class_hex: str = DEMO_CLASS,
    width: int = B64_WIDTH,
    header: str | None = None,
) -> str:
    """Block text in the mock codec's own format (payload is base64 JSON)."""
    payload = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
    item_a = base64.b64encode(b"\x00\x01demo").decode("ascii")
    item_b = base64.b64encode(payload).decode("ascii")
    lines = [header or make_block_header(name=name, module=module, class_hex=class_hex), item_a]
    lines.extend(wrap_text(item_b, width))
    lines.append(">")
    return "\n".join(lines)


def split_block(block_text: str) -> tuple[str, list[str]]:
    """(header_line, payload_lines) of a well-formed mock block."""
    lines = block_text.split("\n")
    if not lines or not lines[0].startswith("<VST ") or lines[-1].rstrip("\r") != ">":
        raise ValueError("not a well-formed VST block")
    return lines[0], [line.rstrip("\r") for line in lines[2:-1]]


def decode_block_document(block_text: str) -> dict[str, Any]:
    _, item_b_lines = split_block(block_text)
    joined = "".join(item_b_lines)
    raw = base64.b64decode(joined + "=" * (-len(joined) % 4))
    brace = raw.find(b"{")
    if brace < 0:
        raise ValueError("no JSON document inside the block payload")
    doc, _ = json.JSONDecoder().raw_decode(raw[brace:].decode("utf-8"))
    return doc


def foreign_block_text(
    *,
    name: str = FOREIGN_NAME,
    module: str = FOREIGN_MODULE,
    class_hex: str = FOREIGN_CLASS,
    payload: Sequence[str] | None = None,
) -> str:
    """A block of another plugin: its payload is NOT our codec's format."""
    lines = [make_block_header(name=name, module=module, class_hex=class_hex)]
    lines.extend(payload or ["Pk9uUFFy", "QUJDREVGR0g", "Zm9yZWlnbi1wYXlsb2Fk", "AAAA"])
    lines.append(">")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Chunk assembly and byte utilities
# ---------------------------------------------------------------------------


def track_chunk(blocks: Iterable[str], *, newline: str = "\n") -> str:
    """Assemble a REAPER-like track chunk around the given blocks."""
    lines = [
        "<TRACK",
        'NAME "Track 1"',
        "PEAKCOL 16576",
        "ISBUS 0 0",
        "<ITEM",
        "POSITION 0",
        "LENGTH 1",
        ">",
    ]
    for block in blocks:
        lines.extend(block.split("\n"))
    lines.append(">")
    return newline.join(lines)


def split_outside(chunk: str, line_start: int, line_end: int) -> tuple[str, str]:
    """Everything before and after a line span, byte-exact."""
    lines = chunk.split("\n")
    return "\n".join(lines[:line_start]), "\n".join(lines[line_end + 1 :])


def byte_diff_span(a: str, b: str) -> tuple[int, int] | None:
    """(first differing index, last differing index + 1) or None if equal."""
    limit = min(len(a), len(b))
    first = None
    for i in range(limit):
        if a[i] != b[i]:
            first = i
            break
    if first is None:
        if len(a) == len(b):
            return None
        return (limit, max(len(a), len(b)))
    last = first
    for i in range(limit - 1, first - 1, -1):
        if a[i] != b[i]:
            last = i
            break
    return (first, max(last + 1, len(a), len(b)))


def char_span_of_lines(chunk: str, line_start: int, line_end: int) -> tuple[int, int]:
    """Character offsets covering lines [line_start, line_end] inclusive."""
    lines = chunk.split("\n")
    before = "\n".join(lines[:line_start])
    start = len(before) + (1 if line_start > 0 else 0)
    span_text = "\n".join(lines[line_start : line_end + 1])
    return start, start + len(span_text)


# ---------------------------------------------------------------------------
# Document path helpers shared by the mock codecs
# ---------------------------------------------------------------------------


def _path_tokens(path: str) -> list[str | int]:
    tokens: list[str | int] = []
    for part in path.split("."):
        name, _, rest = part.partition("[")
        tokens.append(name)
        while rest:
            index_text, _, rest = rest.partition("]")
            tokens.append(int(index_text))
            _, _, rest = rest.partition("[")
    return tokens


def get_path(doc: Any, path: str, *, default: Any = None) -> Any:
    node = doc
    try:
        for token in _path_tokens(path):
            node = node[token]
    except (KeyError, IndexError, TypeError):
        return default
    return node


def set_path(doc: dict[str, Any], path: str, value: Any) -> None:
    tokens = _path_tokens(path)
    if not tokens or not isinstance(tokens[0], str):
        raise ValueError(f"unrooted state path: {path!r}")
    node: Any = doc
    for token, following in zip(tokens, tokens[1:]):
        node = node[token]
    node[tokens[-1]] = value


def flat_paths(value: Any, prefix: str = "") -> dict[str, Any]:
    """Every leaf of a JSON tree as ``dotted[.index]`` -> value."""
    leaves: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, sub in value.items():
            leaves.update(flat_paths(sub, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        for index, sub in enumerate(value):
            leaves.update(flat_paths(sub, f"{prefix}[{index}]"))
    else:
        leaves[prefix] = value
    return leaves


def demo_profile() -> dict[str, Any]:
    """A schema-valid profile for the mock provider (07 §1 shape)."""
    return {
        "profile_schema_version": 1,
        "provider": DEMO_PROVIDER,
        "identity": {
            "plugin_id": DEMO_PROVIDER,
            "vendor": "Demo Audio",
            "format": "vst3",
            "fx_name": DEMO_NAME,
            "class_id": DEMO_CLASS,
            "module": DEMO_MODULE,
            "compatible_versions": ["1.0.0"],
            "fingerprint": {"param_count": len(DEMO_PARAMS)},
        },
        "provenance": {
            "generated_utc": "2026-01-01T00:00:00Z",
            "generator": "demo_provider/tests",
            "provider_revision": "demo-provider-r1",
            "provider_artifact_digest": "sha256:demo-artifacts",
            "sources": [{"file": "demo_registry.json", "sha256": "demo-source-digest"}],
            "source_digest": "sha256:demo-source-digest",
        },
        "modules": [
            {"id": "tone", "kind": "filter", "label": "Tone", "parent": None,
             "controls": ["tone_cutoff", "tone_level", "tone_shape"], "state_refs": ["settings.mode"]},
            {"id": "mix", "kind": "mixer", "label": "Mix", "parent": None,
             "controls": ["mix_percent"], "state_refs": []},
        ],
        "controls": [
            {
                "id": "tone_cutoff", "label": "Tone Cutoff", "module": "tone", "type": "number",
                "unit": "semitones", "range": {"min": 0, "max": 100}, "discrete": False, "aliases": [],
                "authority": {"read": "parameter", "write": "parameter"},
                "binding": {"index": 3, "ident": "3:1003", "state_key": "tone_cutoff"},
                "law": {"kind": "identity", "params": {}},
                "evidence": {"strength": "strong", "source": "demo_fit.json"},
            },
            {
                "id": "tone_level", "label": "Tone Level", "module": "tone", "type": "number",
                "unit": None, "range": {"min": 0, "max": 1}, "discrete": False, "aliases": [],
                "authority": {"read": "parameter", "write": "parameter"},
                "binding": {"index": 4, "ident": "4:1004", "state_key": "tone_level"},
                "law": {"kind": "identity", "params": {}},
                "evidence": {"strength": "strong", "source": "demo_fit.json"},
            },
            {
                "id": "tone_shape", "label": "Tone Shape", "module": "tone", "type": "enum",
                "unit": None, "discrete": True, "aliases": [],
                "enum_values": [{"id": "soft", "label": "Soft"}, {"id": "hard", "label": "Hard"}],
                "authority": {"read": "parameter", "write": "parameter"},
                "binding": {"index": 5, "ident": "5:1005", "state_key": "tone_shape"},
                "law": {"kind": "enum_index", "params": {}},
                "evidence": {"strength": "weak", "source": "demo_fit.json"},
            },
            {
                "id": "mix_percent", "label": "Mix %", "module": "mix", "type": "number",
                "unit": "%", "range": {"min": 0, "max": 100}, "discrete": False, "aliases": [],
                "authority": {"read": "parameter", "write": "parameter"},
                "binding": {"index": 6, "ident": "6:1006", "state_key": "mix_percent"},
                "law": {"kind": "identity", "params": {}},
                "evidence": {"strength": "weak", "source": "demo_fit.json"},
            },
            {
                "id": "mode_state", "label": "Mode", "module": "tone", "type": "state_ref",
                "unit": None, "discrete": True, "aliases": [],
                "enum_values": [{"id": "poly", "label": "Poly"}, {"id": "mono", "label": "Mono"}],
                "authority": {"read": "state", "write": "state"},
                "binding": {"state_key": "settings.mode"},
                "law": {"kind": "identity", "params": {}},
                "evidence": {"strength": "strong", "source": "demo_fit.json"},
            },
        ],
        "values": {"kinds": ["affine", "enum_index", "exponential", "identity", "none"],
                   "unit_traps": ["%"]},
        "modulation": {
            "supported": True, "identity": "slot", "slots": ROUTE_SLOTS,
            "sources": ["src_a", "src_b"], "destinations": ["tone_cutoff", "tone_level"],
            "write_channel": "state", "fields": ["amount", "bipolar"],
            "line_mapping_write_supported": False,
            "structural_limits": {"one_route_per_state_write": True},
        },
        "state": {
            "supported": True, "codec": "b64-json-block-v1",
            "structural_keys": ["settings", "routes"],
            "capture_scope": {
                "channels": ["document", "routes", "parameters"],
                "parameters": {"scope": "full_vector", "ident_required": True},
                "restore_order": ["document", "parameters"],
                "verify_order": ["document", "parameters"],
            },
            "restore_points": "deferred_v1",
        },
        "dependencies": [
            {
                "id": "mode_change_with_active_routes",
                "kind": "hard",
                "informational": False,
                "message": "changing the mode state while routes are active is refused",
                "when": {"state_path": "settings.mode", "min_active_routes": 1},
            }
        ],
        "write_order": ["state", "parameters", "verify"],
        "verification": {
            "tolerances": {"state.float": 1e-05, "parameter.normalized": 0.001},
            "comparators": {"enum": "exact", "number": "abs_le"},
        },
        "limits": ["demo limit: no structural growth", "one route per state write"],
        "x_provider": {},
    }
