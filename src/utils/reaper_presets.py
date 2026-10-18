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

#: MEASURED (2026-10-18, the REAL preset file, all four presets): the blob REAPER stores is
#: `item A (44 bytes) + item B (A[0x20] bytes) + NUL + <preset name> + five NUL bytes`, i.e.
#: `Len == 44 + A[0x20] + len(name) + 6`. Verified on every preset in `vst3-Vital.ini`:
#: BassHouse (15 = 9 + 6), pad (9 = 3 + 6), DirtyBassKongo (20 = 14 + 6), "Bass 808" (14 = 8 + 6).
#: So the name is part of the PAYLOAD, not only of the `Name=` line - a blob written without it is
#: shorter than anything REAPER itself produced, which is why this is modelled instead of assumed.
PRESET_NAME_PREFIX_BYTES = 1
PRESET_NAME_SUFFIX_BYTES = 5

#: The name trailer's total size for a given name (the two NUL runs plus the name).
def name_trailer_bytes(name: str) -> int:
    return PRESET_NAME_PREFIX_BYTES + len(str(name)) + PRESET_NAME_SUFFIX_BYTES


#: MEASURED (2026-10-18): each `Data*` line is `payload part + 1 checksum byte`, so a full line is
#: 16,385 bytes = 32,770 hex characters and carries 16,384 payload bytes. The checksum is
#: `sum(part) & 0xFF` - verified on three consecutive chunks of the real file (`0x87`, `0xcb`,
#: `0x19`), and the model is what makes the payload a DECODABLE document: joining the chunks raw,
#: or dropping their FIRST byte, corrupts the plugin JSON at every boundary (measured: only the
#: "drop the last byte of each chunk" model yields a document with `settings`, 781 keys). `Len` is
#: the payload's true byte length, i.e. `total_chunks*16385 - n_chunks`.
PRESET_PAYLOAD_BYTES = 16384

#: The stored line length for a full chunk (payload + checksum).
PRESET_CHUNK_BYTES = PRESET_PAYLOAD_BYTES + 1

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
        chunks = int(len(preset.get("chunks") or []))
        chars = int(preset.get("data_chars") or 0)
        # MEASURED relation: the stored hex holds the payload PLUS one checksum byte per chunk.
        expected = 2 * ((preset.get("len") or 0) + chunks) if preset.get("len") is not None else None
        if expected is not None and expected != chars:
            problems.append(
                {
                    "line": preset["line"],
                    "reason": "payload_size_disagrees_with_len",
                    "section": preset["section"],
                    "len": preset["len"],
                    "chunks": chunks,
                    "data_chars": chars,
                    "expected_chars": expected,
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
    """One `[PresetK]` section in REAPER's measured shape (hex chunks + checksum, `Len`, `Name`).

    The checksum byte is not decoration: it is what the real file carries at the end of every
    chunk (`sum(part) & 0xFF`), and a section written without it is a file REAPER did not write.
    """
    if not isinstance(index, int) or index < 0:
        raise ValueError(f"a preset index must be a non-negative integer, got {index!r}")
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ValueError("a preset needs its payload bytes")
    if not str(name).strip():
        raise ValueError("a preset needs a non-empty name")
    lines = [f"[{PRESET_SECTION_PREFIX}{index}]"]
    for offset in range(0, len(data), PRESET_PAYLOAD_BYTES):
        key = DATA_KEY_PREFIX if offset == 0 else f"{DATA_KEY_PREFIX}_{offset // PRESET_PAYLOAD_BYTES}"
        part = bytes(data[offset : offset + PRESET_PAYLOAD_BYTES])
        lines.append(f"{key}={(part + bytes([sum(part) & 0xFF])).hex().upper()}")
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


def verify_preset_payload(text: str, section_index: int) -> dict[str, Any]:
    """Decode ONE preset's chunks and check REAPER's own per-chunk checksum byte.

    This is the integrity check the format itself offers (`sum(part) & 0xFF` stored after each
    16,384-byte part), so a payload that survives it is byte-for-byte what REAPER wrote - the
    strongest statement available without asking the host. Returns ``{"found", "bytes",
    "checksums_ok", "bad_chunks", "len_ok", "payload"}``; ``payload`` is the reassembled bytes and
    is only present when every check held.
    """
    parsed = parse_preset_library(text)
    wanted = None
    for preset in parsed["presets"]:
        if preset.get("index") == int(section_index):
            wanted = preset
            break
    if wanted is None:
        return {"found": False}
    keys = sorted(
        wanted.get("chunks") or [],
        key=lambda key: int(key.split("_")[1]) if "_" in key else 0,
    )
    parts: list[bytes] = []
    bad: list[int] = []
    for position, key in enumerate(keys):
        value = _raw_value(text, wanted, key)
        if value is None:
            continue
        blob = bytes.fromhex("".join(value.split()))
        if len(blob) < 2:
            bad.append(position)
            continue
        if (sum(blob[:-1]) & 0xFF) != blob[-1]:
            bad.append(position)
        parts.append(blob[:-1])
    payload = b"".join(parts)
    return {
        "found": True,
        "bytes": len(payload),
        "chunks": len(keys),
        "checksums_ok": not bad,
        "bad_chunks": bad,
        "len_ok": wanted.get("len") == len(payload),
        "payload": payload if not bad else None,
    }


def _raw_value(text: str, preset: dict[str, Any], key: str) -> str | None:
    """The raw hex value of one key inside one section (bounded to that section)."""
    current = None
    for raw in str(text).splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            continue
        if current != preset.get("section") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            return value
    return None


def split_preset_blob(payload: bytes) -> dict[str, Any]:
    """Split a preset blob into the plugin state and REAPER's own name trailer.

    MEASURED: `item A` declares item B's length at offset ``0x20``, so the plugin's state ends at
    ``44 + A[0x20]`` and everything after it is the host's trailer - which the real file shows is
    `NUL + <name> + five NULs`. Returns ``{"core", "artifact", "name", "declared_b", "ok"}``;
    ``ok`` is false when the length field is not usable, in which case the whole payload is
    reported as the core (never silently truncated).
    """
    if not isinstance(payload, (bytes, bytearray)) or len(payload) < 0x24:
        return {"core": bytes(payload or b""), "artifact": b"", "name": None, "declared_b": None, "ok": False}
    blob = bytes(payload)
    declared = int.from_bytes(blob[0x20:0x24], "little")
    end = 44 + declared
    if not 0 < declared or end > len(blob):
        return {"core": blob, "artifact": b"", "name": None, "declared_b": declared or None, "ok": False}
    artifact = blob[end:]
    return {
        "core": blob[:end],
        "artifact": artifact,
        "name": read_name_trailer(artifact),
        "declared_b": declared,
        "ok": True,
    }


def read_name_trailer(artifact: bytes) -> str | None:
    """The preset name REAPER wrote after the state, or ``None`` when the shape is not that.

    MEASURED shape: one NUL, the name, five NULs. A region that does not match returns ``None`` -
    "not understood", never a decoded guess.
    """
    if not isinstance(artifact, (bytes, bytearray)) or len(artifact) < PRESET_NAME_PREFIX_BYTES + PRESET_NAME_SUFFIX_BYTES:
        return None
    blob = bytes(artifact)
    if blob[0] != 0 or blob[-PRESET_NAME_SUFFIX_BYTES:] != b"\x00" * PRESET_NAME_SUFFIX_BYTES:
        return None
    body = blob[PRESET_NAME_PREFIX_BYTES : len(blob) - PRESET_NAME_SUFFIX_BYTES]
    try:
        name = body.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return None
    return name if "\x00" not in name else None


def name_trailer(name: str) -> bytes:
    """REAPER's own name trailer for a preset blob (`NUL + name + five NULs`)."""
    text = str(name)
    if not text:
        raise ValueError("a preset name cannot be empty")
    return b"\x00" + text.encode("utf-8") + b"\x00" * PRESET_NAME_SUFFIX_BYTES


def preset_blob(state: bytes, name: str, *, base: bytes | None = None) -> bytes:
    """The blob REAPER stores for a preset: the plugin state plus ITS OWN name trailer.

    `base` is the state part when the caller already split one (see `split_preset_blob`); when it is
    omitted the whole `state` is used. The trailer is always written fresh for `name`, so a blob
    re-saved under a new name cannot keep the old one.
    """
    core = bytes(base if base is not None else state)
    return core + name_trailer(name)


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
    split = split_preset_blob(item_a + item_b)
    return {
        "header": chosen["header"],
        "class_hex": chosen["class_hex"],
        "module": chosen["module"],
        "ordinal": int(ordinal),
        "matches": len(matching),
        "item_a_bytes": len(item_a),
        "item_b_bytes": len(item_b),
        "data": item_a + item_b,
        # The blob is the plugin's state PLUS whatever REAPER keeps after it - which the measured
        # preset file shows is `NUL + name + five NULs`. The split is reported so a caller saving a
        # preset can replace that name instead of writing a blob shorter than REAPER's own.
        "core_bytes": len(split["core"]),
        "artifact_bytes": len(split["artifact"]),
        "artifact_name": split["name"],
        "blob_ok": split["ok"],
    }


__all__ = [
    "PRESET_CHUNK_BYTES",
    "PRESET_PAYLOAD_BYTES",
    "append_preset",
    "extract_fx_state",
    "name_trailer",
    "name_trailer_bytes",
    "parse_preset_library",
    "preset_blob",
    "preset_names",
    "read_name_trailer",
    "render_preset_section",
    "split_preset_blob",
    "verify_preset_payload",
]
