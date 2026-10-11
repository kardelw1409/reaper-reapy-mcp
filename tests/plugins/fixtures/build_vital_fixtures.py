"""Manual fixture-refresh tool for the pinned Vital fixtures (not part of the suite).

It reads the real registry / recorded POC artifacts from C:\\REAPER and writes
small, self-describing subsets into tests/plugins/fixtures/vital_*.
Every produced file records its source path, the source sha256 and the fact
that it is a subset, so a reader can tell pinned data from generated data.
"""

import ast
import hashlib
import json
import sys
from pathlib import Path

SRC = Path(r"C:\REAPER\prompts\vital_sound_design")
REPO = Path(r"C:\repos\reaper-reapy-mcp")
OUT_REG = REPO / "tests" / "plugins" / "fixtures" / "vital_registry"
OUT_CODEC = REPO / "tests" / "plugins" / "fixtures" / "vital_codec"
OUT_REG.mkdir(parents=True, exist_ok=True)
OUT_CODEC.mkdir(parents=True, exist_ok=True)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print("wrote", path.name, path.stat().st_size, "bytes")


# ---------------------------------------------------------------------------
# 1. registry subsets
# ---------------------------------------------------------------------------

corr_path = SRC / "registry" / "vital_correspondence.json"
ctrl_path = SRC / "registry" / "vital_controls.json"
mods_path = SRC / "registry" / "vital_modules.json"
vocab_path = SRC / "registry" / "modulation_vocabulary.json"
scales_path = SRC / "registry" / "vital_scales_full.json"

corr = load(corr_path)
controls = load(ctrl_path)
modules = load(mods_path)
vocab = load(vocab_path)
scales = load(scales_path)

by_state_key = {row["state_key"]: row for row in corr["rows"] if row.get("state_key")}
destinations = set(vocab["destinations"])

pick: list[str] = []
for kind, count in (("affine", 3), ("enum_index", 2), ("none", 2), ("unmeasured", 1)):
    taken = 0
    for row in corr["rows"]:
        if row.get("kind") == kind and row.get("state_key") in destinations:
            pick.append(row["state_key"])
            taken += 1
            if taken >= count:
                break

converts: list[str] = []
refuses: list[str] = []

#: Recorded in the POC: `modulation_depth("filter_1_cutoff", 8)["amount"] ==
#: 0.0625` (docstring of adapter/capabilities.py), and a recorded REFUSAL set
#: ("eight LFO frequencies, Volume, Reverb Decay Time and Oscillator 1 Unison
#: Detune"). These are pinned as cross-checks for the ported algorithm.
recorded_accept = ["filter_1_cutoff"]
recorded_refuse = ["lfo_1_frequency", "volume", "reverb_decay_time", "osc_1_unison_detune"]
for dest in recorded_accept + recorded_refuse:
    present = dest in destinations
    print("recorded destination", dest, "in vocabulary:", present)
    if present:
        (converts if dest in recorded_accept else refuses).append(dest)
        if dest not in pick:
            pick.append(dest)
for dest in sorted(destinations):
    if len(converts) >= 3 and len(refuses) >= 3:
        break
    if dest in converts or dest in refuses:
        continue
    name = by_state_key.get(dest, {}).get("name")
    if name in scales["scales"] and len(scales["scales"][name].get("states") or []) >= 8:
        (converts if len(converts) < 3 else refuses).append(dest)
print("candidates ->", converts[:5], refuses[:5])

for dest in converts[:3] + refuses[:3]:
    if dest not in pick:
        pick.append(dest)

pick = [key for key in dict.fromkeys(pick) if key]
rows = [copy for key in pick if (copy := by_state_key.get(key))]
indices = {row["index"] for row in rows}
control_rows = [row for row in controls["controls"] if row["index"] in indices]
state_keys = [row["state_key"] for row in rows]
scale_subset = {row["name"]: scales["scales"][row["name"]] for row in rows if row["name"] in scales["scales"]}
module_names = sorted({row["module"] for row in rows if row.get("module")})
module_subset = {name: modules["modules"][name] for name in module_names if name in modules["modules"]}

pairs = vocab["pairs"]


def pair_key(key: str) -> tuple[str, str]:
    if "|" in key:
        a, b = key.split("|", 1)
        return a, b
    if isinstance(pairs, dict) and isinstance(pairs[key], dict):
        return str(pairs[key].get("source", "")), str(pairs[key].get("destination", ""))
    return "", ""


kept_pairs = {}
if isinstance(pairs, dict):
    for key, value in pairs.items():
        source, dest = pair_key(key)
        if dest in set(state_keys):
            kept_pairs[key] = value
    kept_pairs = dict(sorted(kept_pairs.items())[:40])
dest_vocab = {key: value for key, value in vocab["destinations"].items() if key in set(state_keys)}


def meta(source: Path, note: str) -> dict:
    return {
        "_meta": {
            "kind": "pinned subset of a generated registry file",
            "source": str(source),
            "source_sha256": sha256(source),
            "note": note,
            "generated_by": "tests/plugins/fixtures/build_vital_fixtures.py (manual refresh)",
        }
    }


write(
    OUT_REG / "correspondence_subset.json",
    {**meta(corr_path, "rows for the chosen state keys only"), "coverage": corr["coverage"], "kinds": corr["kinds"], "rows": rows},
)
write(
    OUT_REG / "controls_subset.json",
    {
        **meta(ctrl_path, "control rows for the chosen indices only"),
        "fx": controls["fx"],
        "n_params_total": controls["n_params_total"],
        "n_real_controls": controls["n_real_controls"],
        "controls": control_rows,
    },
)
write(
    OUT_REG / "modules_subset.json",
    {**meta(mods_path, "only the modules the chosen controls belong to"), "totals": modules["totals"], "modules": module_subset},
)
write(
    OUT_REG / "scales_subset.json",
    {
        **meta(scales_path, "measured state lists for the chosen controls only"),
        "steps": scales["steps"],
        "n_controls": len(scale_subset),
        "scales": scale_subset,
    },
)
write(
    OUT_REG / "vocabulary_subset.json",
    {
        **meta(vocab_path, "all sources, the chosen destinations, and the pair rows that mention them"),
        "n_sources": vocab["n_sources"],
        "n_destinations": vocab["n_destinations"],
        "n_pairs": vocab["n_pairs"],
        "sources": vocab["sources"],
        "destinations": dest_vocab,
        "pairs": kept_pairs,
        "families": vocab["families"],
        "destinations_without_precedent_for": vocab.get("destinations_without_precedent_for", {}),
        "rules": vocab["rules"],
        "depth_expectations": {
            "recorded_accept": recorded_accept,
            "recorded_refuse": recorded_refuse,
            "candidates_in_subset": {"converts": converts, "refuses": refuses},
            "recorded_example": {
                "destination": "filter_1_cutoff",
                "depth": 8,
                "amount": 0.0625,
                "source": "POC adapter/capabilities.py modulation_depth docstring",
            },
            "measured_by": "the POC docstring's recorded outcome; the fit itself is \n                           \ncomputed by the ported algorithm from scales_subset.json",
        },
    },
)

# ---------------------------------------------------------------------------
# 2. codec fixtures: the recorded state layout + a document in the recorded shape
# ---------------------------------------------------------------------------

blob = load(SRC / "logs" / "b4_state_blob.json")
trailer = ast.literal_eval(blob["B_trailer_repr"])
assert len(trailer) == blob["B_trailer_len"], (len(trailer), blob["B_trailer_len"])
assert len(bytes.fromhex(blob["B_prefix_hex"])) == blob["json_offset_in_B"]
assert len(bytes.fromhex(blob["A_hex"])) == blob["item_a_bytes"]

top_keys = blob["top_keys"]
document = {
    "author": "pinned-fixture",
    "comments": "a Vital-shaped document with the recorded top-level keys",
    "macro1": 0.0, "macro2": 0.0, "macro3": 0.0, "macro4": 0.0,
    "preset_name": "Pinned Fixture",
    "preset_style": "Init",
    "synth_version": "1.5.5",
    "settings": {key: 0.0 for key in state_keys},
    "tuning": {},
}
document["settings"]["modulations"] = [{"destination": "", "source": ""} for _ in range(blob["modulations_count"])]
for n in range(1, blob["modulations_count"] + 1):
    for suffix, value in (("amount", 0.0), ("bipolar", 0.0), ("bypass", 0.0), ("power", 1.0), ("stereo", 0.0)):
        document["settings"][f"modulation_{n}_{suffix}"] = value
for key in top_keys:
    document.setdefault(key, 0.0 if key.startswith("macro") else "")
if state_keys:
    document["settings"]["modulations"][0] = {"destination": state_keys[0], "source": "lfo_1"}
    document["settings"]["modulation_1_amount"] = -0.25

write(
    OUT_CODEC / "state_layout.json",
    {
        "_meta": {
            "kind": "recorded measurements of the live Vital state chunk",
            "source": str(SRC / "logs" / "b4_state_blob.json"),
            "source_sha256": sha256(SRC / "logs" / "b4_state_blob.json"),
            "note": ("the full chunk bytes were not archived; this pins every recorded "
                     "measurement plus item A, the 184-byte item-B prefix and the 40-byte "
                     "trailer, so a fixture built from them has the recorded layout"),
        },
        "chunk_len": blob["chunk_len"],
        "vst_line": blob["vst_line"],
        "end_line": blob["end_line"],
        "item_a_bytes": blob["item_a_bytes"],
        "item_b_bytes": blob["item_b_bytes"],
        "item_a_chars": blob["item_a_chars"],
        "item_b_chars": blob["item_b_chars"],
        "json_offset_in_B": blob["json_offset_in_B"],
        "json_bytes": blob["json_bytes"],
        "b_trailer_len": blob["B_trailer_len"],
        "b64_width": 280,
        "b64_artifact": "AAAAAAAA",
        "settings_count": blob["settings_count"],
        "modulations_count": blob["modulations_count"],
        "top_keys": top_keys,
        "item_a_hex": blob["A_hex"],
        "item_b_prefix_hex": blob["B_prefix_hex"],
        "item_b_trailer_hex": trailer.hex(),
        "b_trailer_repr": blob["B_trailer_repr"],
        "roundtrip": blob["roundtrip"],
        "length_fields": {
            "A[0x20:0x24]": "little-endian u32 = len(item B)",
            "B[0x00:0x04]": "little-endian u32 = len(item B) - 16",
        },
    },
)
write(OUT_CODEC / "document.json", document)
print("state_keys:", len(state_keys), state_keys)
print("scales kept:", list(scale_subset))
