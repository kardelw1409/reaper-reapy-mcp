"""REAPER's own FX preset store: read it, and write into it in REAPER's exact format.

MEASURED (2026-10-18, this machine's REAPER resource path, `presets/vst3-Vital.ini`, 3,912,003
bytes, 140 lines, 4 presets):

    [General]
    NbPresets=4

    [Preset0]
    Data=<uppercase hex, 32770 characters>
    Data_1=<uppercase hex>
    ...
    Data_N=<52 or 32770 characters>
    Len=175762
    Name=BassHouse

    [Preset1]
    ...

* a preset's payload is split into **16,385-byte** chunks -> 32,770 hex characters per line, keys
  `Data`, `Data_1`, `Data_2`, ... (the first chunk of every preset measured is the full 32,770; the
  last one is short);
* `Len` is the payload's total size in BYTES (175,762 for `BassHouse`, 1,428,230 for
  `DirtyBassKongo`), so it verifies a parse without decoding anything;
* `NbPresets` counts the sections (4 sections = 0..3);
* the decoded payload starts `a0e38a6e ee5eedfe ...` and reaches the ASCII `VstW` at ~offset 48 -
  the same wrapper the plugin state carries inside REAPER's chunk (`VstW` is REAPER's own item-B
  header, measured in the plugin layer as well), i.e. the file stores the FX's VST3 state.

Why this module is pure: the format is a fact about REAPER, the parsing is the part that can be
tested without a host, and the controller on top of it only does host calls. Nothing here imports
reapy, so the offline suite can hold the format still while the live probe checks the channel.
"""

from __future__ import annotations

import base64
from typing import Any

#: MEASURED: every `Data*` line of a full chunk is 32,770 hex characters = 16,385 bytes.
PRESET_CHUNK_BYTES = 16385

#: The general section and the key that counts the presets (measured).
GENERAL_SECTION = "General"
NB_PRESETS_KEY = "NbPresets"

#: A preset section is `[PresetK]`; the payload keys are `Data`, `Data_1`, ... (measured).
PRESET_SECTION_PREFIX = "Preset"
DATA_KEY_PREFIX = "Data"
LEN_KEY = "Len"
NAME_KEY = "Name"


def _int_or_none(text: str) -> int | None:
    text = str(text).strip()
    return int(text) if text.isdigit() else None


def parse_preset_library(text: str) -> dict[str, Any]:
    """Parse REAPER's preset file into counts and names - never the payloads.

    Returns ``{"nb_presets", "presets": [...], "problems": [...]}``. A problem is a line that
    could not be read as the measured format; it is REPORTED rather than skipped, because a file
    this module does not understand must not be written back to (the caller uses `problems` to
    refuse).
    """
    problems: list[dict[str, Any]] = []
    nb_presets: int | None = None
    presets: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for number, raw in enumerate(str(text).splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            if current is not None and current.get("section") != GENERAL_SECTION:
                presets.append(current)
            section = line[1:-1].strip()
            current = {"section": section, "line": number}
            if section.startswith(PRESET_SECTION_PREFIX):
                current["index"] = _int_or_none(section[len(PRESET_SECTION_PREFIX) :])
            continue
        if "=" not in line:
            problems.append({"line": number, "reason": "not_a_key_value", "text": line[:60]})
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if current is None:
            problems.append({"line": number, "reason": "key_outside_a_section", "key": key})
            continue
        if current["section"] == GENERAL_SECTION:
            if key == NB_PRESETS_KEY:
                nb_presets = _int_or_none(value)
                if nb_presets is None:
                    problems.append({"line": number, "reason": "nb_presets_not_a_number", "text": value[:60]})
            continue
        if key.startswith(DATA_KEY_PREFIX):
            chunks = current.setdefault("chunks", [])
            chunks.append(key)
            current["data_chars"] = int(current.get("data_chars") or 0) + len(value)
        elif key == LEN_KEY:
            current["len"] = _int_or_none(value)
        elif key == NAME_KEY:
            current["name"] = value
    if current is not None and current.get("section") != GENERAL_SECTION:
        presets.append(current)
    for preset in presets:
        if preset.get("index") is None:
            problems.append({"line": preset["line"], "reason": "section_index_unreadable", "section": preset["section"]})
        if "name" not in preset:
            problems.append({"line": preset["line"], "reason": "preset_without_name", "section": preset["section"]})
        expected = (preset.get("len") or 0) * 2 if preset.get("len") else None
        if expected is not None and expected != int(preset.get("data_chars") or 0):
            problems.append(
                {
                    "line": preset["line"],
                    "reason": "payload_size_disagrees_with_len",
                    "section": preset["section"],
                    "len": preset["len"],
                    "data_chars": int(preset.get("data_chars") or 0),
                }
            )
    return {"nb_presets": nb_presets, "presets": presets, "problems": problems}


def preset_names(text: str) -> list[dict[str, Any]]:
    """The readable rows of a preset file: index, name, byte size - no payloads."""
    parsed = parse_preset_library(text)
    return [
        {
            "index": preset.get("index"),
            "name": preset.get("name"),
            "bytes": preset.get("len"),
            "section": preset.get("section"),
        }
        for preset in parsed["presets"]
    ]


def render_preset_section(index: int, name: str, data: bytes) -> list[str]:
    """One `[PresetK]` section in REAPER's measured shape (hex chunks, `Len`, `Name`)."""
    if not isinstance(index, int) or index < 0:
        raise ValueError(f"a preset index must be a non-negative integer, got {index!r}")
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ValueError("a preset needs its payload bytes")
    if not str(name).strip():
        raise ValueError("a preset needs a non-empty name")
    lines = [f"[{PRESET_SECTION_PREFIX}{index}]"]
    for offset in range(0, len(data), PRESET_CHUNK_BYTES):
        key = DATA_KEY_PREFIX if offset == 0 else f"{DATA_KEY_PREFIX}_{offset // PRESET_CHUNK_BYTES}"
        chunk = bytes(data[offset : offset + PRESET_CHUNK_BYTES])
        lines.append(f"{key}={chunk.hex().upper()}")
    lines.append(f"{LEN_KEY}={len(data)}")
    lines.append(f"{NAME_KEY}={name}")
    return lines


def append_preset(text: str, name: str, data: bytes) -> dict[str, Any]:
    """Add one preset to a REAPER preset file, in REAPER's own order and shape.

    Returns ``{"text", "index", "section", "nb_presets_before", "nb_presets_after"}``. Refuses (by
    raising) when the file could not be parsed cleanly: appending to a file whose format is not the
    measured one would corrupt the owner's preset library. The payload is appended, never re-encoded,
    so every existing byte of every existing preset survives verbatim.
    """
    parsed = parse_preset_library(text)
    if parsed["problems"]:
        raise ValueError(
            "refusing to append: the preset file does not match the measured format"
        )
    indexes = [preset.get("index") for preset in parsed["presets"]]
    if any(index is None for index in indexes):
        raise ValueError("refusing to append: a preset section has no readable index")
    index = (max(indexes) + 1) if indexes else 0
    nb_before = parsed["nb_presets"]
    nb_after = max(index + 1, nb_before or 0)
    lines = str(text).splitlines()
    replaced = False
    for position, raw in enumerate(lines):
        if raw.strip().startswith(f"{NB_PRESETS_KEY}="):
            lines[position] = f"{NB_PRESETS_KEY}={nb_after}"
            replaced = True
            break
    if not replaced:
        raise ValueError(f"refusing to append: no {NB_PRESETS_KEY} key to update")
    body = "\n".join(lines).rstrip("\n")
    body += "\n\n" + "\n".join(render_preset_section(index, name, data)) + "\n"
    return {
        "text": body,
        "index": index,
        "section": f"{PRESET_SECTION_PREFIX}{index}",
        "nb_presets_before": nb_before,
        "nb_presets_after": nb_after,
    }


# ---------------------------------------------------------------------------
# Reading the FX's own state out of a track chunk (the payload a preset stores)
# ---------------------------------------------------------------------------


def _b64(value: str) -> bytes:
    text = "".join(value.split())
    return base64.b64decode(text + "=" * (-len(text) % 4))


def _header_fields(line: str) -> dict[str, Any]:
    """The class id and module of a `<VST ...>` block header (measured layout)."""
    body = line[len("<VST") :].strip().rstrip(">").strip()
    fields: list[str] = []
    current = ""
    quoted = False
    for char in body:
        if char == '"':
            quoted = not quoted
            continue
        if char == " " and not quoted:
            if current:
                fields.append(current)
            current = ""
            continue
        current += char
    if current:
        fields.append(current)
    class_hex = None
    for field in fields:
        text = field.strip("{}")
        if len(text) == 32 and all(ch in "0123456789abcdefABCDEF" for ch in text):
            class_hex = text.upper()
            break
    module = None
    if fields:
        module = fields[1] if len(fields) > 1 else None
    return {"fields": fields, "class_hex": class_hex, "module": module}


def extract_fx_state(
    chunk: str,
    ordinal: int,
    *,
    class_hex: str | None = None,
    module: str | None = None,
) -> dict[str, Any]:
    """The addressed FX's own state bytes from a track chunk (item A + item B).

    MEASURED layout of a plugin block inside REAPER's chunk: the header line `<VST "name" module
    ... {classid} ""`, then item A as one base64 line, then item B across the following line(s),
    then a line holding exactly ``>``. The block's payload is item A followed by item B - the same
    bytes a REAPER preset stores (the preset file's payload reaches the `VstW` marker item B
    carries, measured on `vst3-Vital.ini`).

    Refuses rather than guesses: no matching block, an unreadable header, an unterminated block or
    an ordinal outside the matches all raise `ValueError` naming what was seen.
    """
    lines = str(chunk).split("\n")
    blocks: list[dict[str, Any]] = []
    position = 0
    while position < len(lines):
        line = lines[position]
        if not line.startswith("<VST"):
            position += 1
            continue
        end = None
        for scan in range(position + 1, len(lines)):
            if lines[scan].strip() == ">":
                end = scan
                break
        if end is None:
            raise ValueError(
                f"the VST block at line {position} is not terminated (no closing '>')"
            )
        blocks.append({"start": position, "end": end, "lines": lines[position : end + 1]})
        position = end + 1
    if not blocks:
        raise ValueError("this chunk carries no <VST ...> block")
    matching: list[dict[str, Any]] = []
    for block in blocks:
        head_text = block["lines"][0]
        fields = _header_fields(head_text)
        block["header"] = head_text
        block["class_hex"] = fields["class_hex"]
        block["module"] = fields["module"]
        if class_hex is not None and (block["class_hex"] or "").upper() != str(class_hex).upper():
            continue
        if module is not None and block["module"] != module:
            continue
        matching.append(block)
    if not matching:
        raise ValueError(
            "no block matches the requested identity",
        )
    if not 0 <= int(ordinal) < len(matching):
        raise ValueError(
            f"the ordinal {ordinal} cannot address any of the {len(matching)} matching block(s)"
        )
    chosen = matching[int(ordinal)]
    if len(chosen["lines"]) < 3:
        raise ValueError("the block carries no item A/item B lines")
    item_a_b64 = chosen["lines"][1].strip()
    item_b_b64 = "".join(part.strip() for part in chosen["lines"][2:])
    item_a = _b64(item_a_b64)
    item_b = _b64(item_b_b64)
    return {
        "header": chosen["header"],
        "class_hex": chosen["class_hex"],
        "module": chosen["module"],
        "ordinal": int(ordinal),
        "matches": len(matching),
        "item_a_bytes": len(item_a),
        "item_b_bytes": len(item_b),
        "data": item_a + item_b,
    }


__all__ = [
    "PRESET_CHUNK_BYTES",
    "append_preset",
    "extract_fx_state",
    "parse_preset_library",
    "preset_names",
    "render_preset_section",
]
