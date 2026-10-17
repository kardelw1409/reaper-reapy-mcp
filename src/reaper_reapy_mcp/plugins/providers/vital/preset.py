"""Vital provider: the plugin's NATIVE preset library (`.vital` files).

This is the plugin's own browser library - NOT the host's REAPER presets, which
the plugin does not expose (measured live 2026-10-16: every host-visible preset
channel answers empty/`-1` on Vital).

**Portability.** The data root is resolved in the plugin's own order and nothing
here is machine-specific:

1. `REAPER_MCP_VITAL_DATA_DIR` - an explicit override (tests, non-standard installs);
2. the `data_directory` key of the plugin's own config file, found in the
   platform application-data directory (both the `Vital` and the legacy `Vial`
   directory names are tried);
3. the platform default the plugin itself falls back to (Windows
   `~/Documents/Vital`, macOS `~/Music/Vital`, Linux
   `$XDG_DATA_HOME`/`~/.local/share` + `vital`).

A preset is identified by its path RELATIVE to that root (posix separators): the
plugin's own browser identifies presets by file name (measured: it takes the name
from the file, and 549 of 2226 corpus files carry no embedded `preset_name`), so
the relative path is the stable identity and the display name is the file stem.

**What an apply can carry (all measured live, P38 2026-10-16/17, build 1.5.5):**

- control values, element-wise `modulations` / `lfos` / `custom_warps` /
  `random_values` - land and verify;
- all eight top-level labels (`preset_name`, `author`, `comments`,
  `preset_style`, `macro1..4`) - land and read back inside the write budget;
- wavetable slots - each whole-table write is bounded by the codec's measured
  growth guard, so a slot that would grow the engine's table is declined HERE
  (before the plan) and reported as a gap, never as a failed whole apply;
- the sample block - carried only when its payload fits the measured write
  budget (the codec re-checks and declines it with a notice otherwise);
- `tuning` is document-only (no preset carries it) and `beats_per_minute` is
  host-owned (the plugin follows the project tempo) - both are named gaps;
- a routing whose DESTINATION the running build no longer knows is accepted by
  the host and then dropped by the plugin on re-serialisation (the slot reads
  back empty). Such a pair is named per apply and remembered per build in
  `vital_destination_drops.json`, so later applies skip it up front - see
  "Destinations the plugin drops" below.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ...errors import InvalidValue, PluginError

__all__ = [
    "DATA_DIR_ENV",
    "PRESET_EXT",
    "LABEL_KEYS",
    "BLOCK_KEYS",
    "HOST_OWNED_KEYS",
    "DROPS_FILE",
    "data_dir",
    "config_file_candidates",
    "platform_data_defaults",
    "relative_id",
    "safe_join",
    "read_preset_metadata",
    "load_preset",
    "read_dropped_destinations",
    "known_dropped_destinations",
    "record_dropped_destinations",
    "change_set",
    "save_document",
    "PresetCapability",
]

#: Environment override for the plugin's data root.
DATA_DIR_ENV = "REAPER_MCP_VITAL_DATA_DIR"

PRESET_EXT = ".vital"

#: Top-level metadata keys a preset carries (writable labels - measured).
LABEL_KEYS: tuple[str, ...] = (
    "preset_name",
    "author",
    "comments",
    "preset_style",
    "macro1",
    "macro2",
    "macro3",
    "macro4",
)

#: `settings` sub-objects that are not controls (structure, element-wise).
BLOCK_KEYS = ("sample", "wavetables", "lfos", "modulations", "custom_warps", "random_values")

#: Settings keys the HOST owns: the plugin follows the project tempo, so a preset
#: value for it is not applicable (measured 2026-10-16: the write is ignored).
HOST_OWNED_KEYS = ("beats_per_minute",)

#: The element-wise list blocks (each entry addressable by index).
ELEMENT_BLOCKS = ("modulations", "lfos", "custom_warps", "random_values")

_HEAD_TAIL_BYTES = 4096
_NAME_RE = re.compile(r'"preset_name"\s*:\s*"([^"]*)"')
_AUTHOR_RE = re.compile(r'"author"\s*:\s*"([^"]*)"')
_STYLE_RE = re.compile(r'"preset_style"\s*:\s*"([^"]*)"')
_VERSION_RE = re.compile(r'"synth_version"\s*:\s*"([^"]*)"')
_UNSAFE_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Portable path resolution
# ---------------------------------------------------------------------------


def _platform_appdata(env: Mapping[str, str]) -> Path:
    if sys.platform.startswith("win"):
        local = env.get("APPDATA")
        return Path(local) if local else Path.home() / "AppData" / "Roaming"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(env.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))


def _platform_music_data(env: Mapping[str, str]) -> Path:
    if sys.platform.startswith("win"):
        return Path.home() / "Documents"
    if sys.platform == "darwin":
        return Path.home() / "Music"
    return Path(env.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))


def platform_data_defaults(env: Mapping[str, str] | None = None) -> list[Path]:
    """Candidate data roots, newest naming first (`Vital`), legacy second (`Vial`)."""
    environ = os.environ if env is None else env
    base = _platform_music_data(environ)
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return [base / "Vital", base / "Vial"]
    return [base / "vital", base / "Vial"]


def config_file_candidates(env: Mapping[str, str] | None = None) -> list[Path]:
    """The plugin's config files to try, in order (application data dir)."""
    environ = os.environ if env is None else env
    appdata = _platform_appdata(environ)
    out: list[Path] = []
    for folder in ("Vital", "Vial"):
        for name in ("Vital.config", "Vial.config"):
            candidate = appdata / folder / name
            if candidate not in out:
                out.append(candidate)
    return out


def data_dir(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the plugin's preset data root (override -> config key -> platform).

    The override is honoured verbatim (like the discovery directory's override):
    an explicitly configured path is the caller's decision, not ours to second
    guess. Without it, the plugin's own config wins, then the platform default.
    """
    environ = os.environ if env is None else env
    override = environ.get(DATA_DIR_ENV)
    if override is not None:
        if not str(override).strip():
            raise InvalidValue(
                f"{DATA_DIR_ENV} is set to an empty value; a literal path is required",
                detail={"env": DATA_DIR_ENV},
            )
        return Path(os.path.normpath(os.path.abspath(str(override))))
    for config in config_file_candidates(environ):
        if not config.is_file():
            continue
        try:
            payload = json.loads(config.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        configured = payload.get("data_directory") if isinstance(payload, dict) else None
        if isinstance(configured, str) and configured.strip():
            return Path(os.path.normpath(os.path.abspath(configured)))
    candidates = platform_data_defaults(environ)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def relative_id(path: Path, root: Path) -> str:
    """A preset's stable id: its path relative to the data root (posix)."""
    return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()


def safe_join(root: Path, preset_id: str) -> Path:
    """Resolve a preset id INSIDE the data root, or refuse (path traversal).

    An id that climbs out of the root, is absolute, or does not name an existing
    `.vital` file is a typed refusal - a preset catalog must never read or write
    outside its own library.
    """
    text = str(preset_id).strip().replace("\\", "/")
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text) or ".." in text.split("/"):
        raise InvalidValue(
            f"preset id {preset_id!r} is not a relative path inside the preset library",
            detail={"id": str(preset_id), "reason": "unacceptable_id"},
        )
    root = Path(root).resolve()
    candidate = (root / text).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:  # pragma: no cover - covered by the `..` check above
        raise InvalidValue(
            f"preset id {preset_id!r} escapes the preset library root",
            detail={"id": str(preset_id), "root": str(root)},
        ) from exc
    if not candidate.is_file():
        raise InvalidValue(
            f"no preset file for id {preset_id!r}",
            detail={"id": str(preset_id), "root": str(root), "reason": "not_found"},
        )
    if candidate.suffix.lower() != PRESET_EXT:
        raise InvalidValue(
            f"preset id {preset_id!r} does not name a {PRESET_EXT} file",
            detail={"id": str(preset_id), "suffix": candidate.suffix},
        )
    return candidate


# ---------------------------------------------------------------------------
# Reading presets
# ---------------------------------------------------------------------------


def read_preset_metadata(path: Path) -> dict[str, Any]:
    """Metadata from a preset's head/tail (the fields the writer puts last).

    Cheap by design: the writer serializes a single line, so `synth_version`
    (alphabetically last) sits in the tail and the metadata keys in the head -
    4 KB from each end is enough for the entire corpus (measured: 0.6 s for 2226
    files).
    """
    stat = path.stat()
    head = b""
    tail = b""
    with open(path, "rb") as handle:
        head = handle.read(_HEAD_TAIL_BYTES)
        size = stat.st_size
        if size > _HEAD_TAIL_BYTES:
            try:
                handle.seek(-_HEAD_TAIL_BYTES, os.SEEK_END)
            except OSError:
                handle.seek(0)
            tail = handle.read(_HEAD_TAIL_BYTES)
    head_text = head.decode("utf-8", "replace")
    tail_text = tail.decode("utf-8", "replace")
    haystack = head_text + "\n" + tail_text

    def pick(rx: re.Pattern[str]) -> str:
        found = rx.search(haystack)
        return found.group(1) if found else ""

    return {
        "size": size,
        "mtime_ns": stat.st_mtime_ns,
        "name": path.stem,
        "embedded_name": pick(_NAME_RE),
        "author": pick(_AUTHOR_RE),
        "style": pick(_STYLE_RE),
        "synth_version": pick(_VERSION_RE),
    }


def load_preset(path: Path) -> dict[str, Any]:
    """A preset file as its document dict, validated by shape."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        raise InvalidValue(
            f"the preset file could not be read as JSON: {exc}",
            detail={"path": str(path), "reason": "unreadable_json"},
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("settings"), dict):
        raise InvalidValue(
            "the preset file is not a Vital document (no `settings` object)",
            detail={"path": str(path), "reason": "not_a_document"},
        )
    return payload


# ---------------------------------------------------------------------------
# The change set: preset file + live document -> state paths
# ---------------------------------------------------------------------------


def _diff_element(block: str, live_block: Any, file_block: Any, paths: dict[str, Any]) -> int:
    """Emit element-wise paths for a list block; returns how many were emitted."""
    if not isinstance(file_block, list):
        return 0
    live_list = live_block if isinstance(live_block, list) else []
    emitted = 0
    for index, entry in enumerate(file_block):
        if index < len(live_list) and live_list[index] == entry:
            continue
        if isinstance(entry, dict):
            # element-wise fields keep each write inside the growth budget and
            # make the verification rows name the field that changed
            live_entry = live_list[index] if index < len(live_list) and isinstance(live_list[index], dict) else {}
            for field, value in entry.items():
                if live_entry.get(field) != value:
                    paths[f"settings.{block}.{index}.{field}"] = value
                    emitted += 1
        else:
            paths[f"settings.{block}.{index}"] = entry
            emitted += 1
    return emitted


def _growth_gap(document: dict[str, Any], path: str, value: Any) -> int | None:
    """The growth of a single path, or None when it is inside the write band.

    Measured (P38, 2026-10-17): a write whose own growth exceeds the channel's
    band is accepted by the host and silently IGNORED by the plugin - the value
    reads back unchanged. A preset's LFO curves are the common case (the file's
    LFO has more points than the live one, so the array grows by hundreds of
    characters), and no batching can split one leaf. The value is therefore not
    carried and the caller names it - the codec enforces the same limit as a
    second net (it must hold for any caller, not only a preset apply).
    """
    from . import codec as codec_module

    growth = codec_module._growth_of(document, path, value)
    if growth > codec_module.SINGLE_PATH_GROWTH_LIMIT:
        return int(growth)
    return None


def _route_ops_from_modulations(
    file_settings: dict[str, Any],
    live_settings: dict[str, Any],
    dropped_destinations: Any = (),
) -> dict[str, Any]:
    """Map the preset's `modulations` list onto the route-operation channel (P38).

    The measured reason this is not a state-path diff: the transaction's
    `route_into_off_filter` rule decides a created pair from the pair's
    destination, and a state-path write of `source` alone is **undecidable**
    against a slot whose live destination is empty - so an element-wise write of
    the pair fields is refused for exactly the slots a preset activates. The
    route operations carry the pair explicitly, which is what the evaluator
    reads, and a create may name the FILE's own slot so the per-slot
    `modulation_N_*` amounts keep pairing with their own routing.
    """
    file_mods = file_settings.get("modulations") if isinstance(file_settings.get("modulations"), list) else []
    live_mods = live_settings.get("modulations") if isinstance(live_settings.get("modulations"), list) else []
    dropped = {str(name) for name in dropped_destinations or ()}
    creates: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    removes: list[dict[str, Any]] = []
    counters = {
        "routes_create": 0,
        "routes_update": 0,
        "routes_remove": 0,
        "routes_skipped_known_drop": 0,
        "inactive_slot_text_skipped": 0,
        "modulation_curves_not_carried": 0,
    }
    gaps: list[dict[str, Any]] = []
    for index, entry in enumerate(file_mods):
        slot = index + 1
        file_slot = entry if isinstance(entry, dict) else {}
        live_slot = live_mods[index] if index < len(live_mods) and isinstance(live_mods[index], dict) else {}
        file_source = str(file_slot.get("source") or "")
        file_destination = str(file_slot.get("destination") or "")
        live_source = str(live_slot.get("source") or "")
        live_destination = str(live_slot.get("destination") or "")
        if (file_source, file_destination) == (live_source, live_destination):
            continue
        if file_destination and file_destination in dropped:
            # Measured on an earlier apply of THIS build: the plugin takes the write
            # and re-serialises the slot empty. Writing it again would only repeat the
            # same dance (and the same rollback-free drop), so the routing is named
            # and left out - which is exactly what the plugin's own load does.
            counters["routes_skipped_known_drop"] += 1
            gaps.append(
                {
                    "item": f"modulations[{index}]",
                    "reason": "destination_dropped_by_plugin",
                    "detail": (
                        "this build's plugin accepted and then dropped this destination on an "
                        "earlier apply (the slot reads back empty; measured), so the routing is not "
                        "written again"
                    ),
                    "destination": file_destination,
                }
            )
            continue
        if file_slot.get("line_mapping"):
            # the provider declares `no line_mapping writes`; a preset that carries a
            # non-linear modulation curve applies with the curve's DEFAULT shape, and
            # that partial fidelity is named rather than silently assumed
            counters["modulation_curves_not_carried"] += 1
        if not file_destination:
            if live_destination or live_source:
                removes.append({"slot": slot})
                counters["routes_remove"] += 1
            else:
                # both slots are inactive: the difference is the inactive slot's own
                # text, which has no audible effect (the plugin keys activity on the
                # destination). Not written, and counted so the outcome can say so.
                counters["inactive_slot_text_skipped"] += 1
            continue
        spec: dict[str, Any] = {
            "slot": slot,
            "source": file_source,
            "destination": file_destination,
        }
        amount = file_settings.get(f"modulation_{slot}_amount")
        if amount is not None:
            spec["amount"] = amount
        else:
            gaps.append(
                {
                    "item": f"modulations[{index}]",
                    "reason": "route_amount_missing",
                    "detail": (
                        "the preset carries an active routing without its "
                        f"`modulation_{slot}_amount` control, so the route's depth cannot be stated"
                    ),
                }
            )
            continue
        bipolar = file_settings.get(f"modulation_{slot}_bipolar")
        if bipolar is not None:
            spec["bipolar"] = bipolar
        if live_destination:
            updates.append(spec)
            counters["routes_update"] += 1
        else:
            creates.append(spec)
            counters["routes_create"] += 1
    if counters["modulation_curves_not_carried"]:
        gaps.append(
            {
                "item": "modulations[*].line_mapping",
                "reason": "modulation_curve_not_carried",
                "detail": (
                    f"{counters['modulation_curves_not_carried']} routing(s) carry a non-linear "
                    "modulation curve; the provider declares no line_mapping writes, so those "
                    "routes apply with the default (linear) shape"
                ),
            }
        )
    return {"creates": creates, "updates": updates, "removes": removes, "counters": counters, "gaps": gaps}


def change_set(
    document: dict[str, Any],
    preset: dict[str, Any],
    *,
    data_dir_path: Path | None = None,
    preset_id: str | None = None,
    guarded_paths: Any = (),
    dropped_destinations: Any = (),
) -> dict[str, Any]:
    """The state change set that applies `preset` onto the live `document`.

    Only DIFFERENCES are emitted (the live document is in hand): an apply touches
    the paths the preset actually changes, which keeps the write chain short and
    the verification focused. Every part the apply cannot carry is returned as a
    named gap with the measurement behind it - never silently dropped.

    `guarded_paths` names the state paths a declared-but-not-evaluated hard rule
    refuses (the provider derives them; see `guarded_state_paths`): writing one
    would make the WHOLE transaction refuse, so the preset's value for it is
    skipped here and named as a gap instead.

    `dropped_destinations` names the pair DESTINATIONS this build's plugin was
    measured to drop (the memo written by earlier applies): those routings are
    skipped up front and named, instead of being written to be dropped again.
    """
    from . import codec as codec_module

    guarded = {str(path) for path in guarded_paths or ()}
    live_settings = document.get("settings") if isinstance(document.get("settings"), dict) else {}
    file_settings = preset.get("settings") if isinstance(preset.get("settings"), dict) else {}

    paths: dict[str, Any] = {}
    gaps: list[dict[str, Any]] = []
    counts = {
        "controls": 0,
        "lfos": 0,
        "custom_warps": 0,
        "random_values": 0,
        "labels": 0,
        "sample": 0,
        "guarded_skipped": 0,
        "growth_over_budget": 0,
        "wavetables_requested": 0,
        "wavetables_declined": 0,
    }

    def carry(path: str, value: Any, counter: str | None = None) -> bool:
        """Add a path when it fits the measured single-write band; else name it."""
        growth = _growth_gap(document, path, value)
        if growth is None:
            paths[path] = value
            if counter:
                counts[counter] += 1
            return True
        counts["growth_over_budget"] += 1
        gaps.append(
            {
                "item": path,
                "reason": "growth_over_budget",
                "detail": (
                    "the preset's value grows the document beyond the measured single-write "
                    "band, and a write above it is silently ignored by the plugin (it reads "
                    "back unchanged), so this part of the preset is not carried"
                ),
                "growth_chars": growth,
                "limit_chars": codec_module.SINGLE_PATH_GROWTH_LIMIT,
            }
        )
        return False

    for key, value in file_settings.items():
        if key in BLOCK_KEYS:
            continue
        state_path = f"settings.{key}"
        if key in HOST_OWNED_KEYS:
            if live_settings.get(key) != value:
                gaps.append(
                    {
                        "item": state_path,
                        "reason": "host_owned_parameter",
                        "detail": (
                            "the host owns this value (the plugin follows the project tempo), "
                            "so a preset value for it is not applicable - measured 2026-10-16"
                        ),
                        "preset_value": value,
                    }
                )
            continue
        if state_path in guarded:
            if live_settings.get(key) != value:
                counts["guarded_skipped"] += 1
                gaps.append(
                    {
                        "item": state_path,
                        "reason": "guarded_rule_subject",
                        "detail": (
                            "a declared hard rule refuses any plan that enters this setting while "
                            "the rule is not evaluable (fm_needs_modulator); the preset's value is "
                            "not applied so the rest of the preset can land"
                        ),
                        "preset_value": value,
                    }
                )
            continue
        if live_settings.get(key) != value:
            carry(state_path, value, "controls")

    for block in ("lfos", "custom_warps", "random_values"):
        before = set(paths)
        _diff_element(block, live_settings.get(block), file_settings.get(block), paths)
        added = [row for row in paths if row not in before]
        elements: dict[str, list[str]] = {}
        for path in added:
            elements.setdefault(path.rsplit(".", 1)[0], []).append(path)
        kept = 0
        for element, element_paths in elements.items():
            # ONE element (a curve: its point count, its points and its powers) is
            # carried WHOLE or not at all - a count that lands while its array is
            # dropped would leave the live curve in a state the preset never had.
            over = []
            for path in element_paths:
                growth = _growth_gap(document, path, paths[path])
                if growth is not None:
                    over.append((path, growth))
            worst = max(over, key=lambda row: row[1]) if over else None
            if worst is not None:
                for path in element_paths:
                    del paths[path]
                counts["growth_over_budget"] += 1
                gaps.append(
                    {
                        "item": element,
                        "reason": "growth_over_budget",
                        "detail": (
                            "one of this element's values grows the document beyond the measured "
                            "single-write band (a write above it is silently ignored by the "
                            "plugin), and an element is carried whole or not at all - so the "
                            "live curve is left as it is"
                        ),
                        "field": worst[0],
                        "growth_chars": worst[1],
                        "limit_chars": codec_module.SINGLE_PATH_GROWTH_LIMIT,
                    }
                )
                continue
            kept += len(element_paths)
        counts[block] = kept

    route_plan = _route_ops_from_modulations(file_settings, live_settings, dropped_destinations)
    counts.update(route_plan["counters"])
    gaps.extend(route_plan["gaps"])

    for key in LABEL_KEYS:
        if key in preset and preset.get(key) != document.get(key):
            carry(key, preset.get(key), "labels")

    wavetable_specs: list[dict[str, Any]] = []
    file_tables = file_settings.get("wavetables")
    live_tables = live_settings.get("wavetables")
    if isinstance(file_tables, list):
        for index, table in enumerate(file_tables):
            counts["wavetables_requested"] += 1
            current = live_tables[index] if isinstance(live_tables, list) and index < len(live_tables) else None
            try:
                from . import wavetables as wavetables_module

                wavetables_module.check_writable(
                    table, current=current, live_version=document.get("synth_version")
                )
            except PluginError as exc:
                counts["wavetables_declined"] += 1
                gaps.append(
                    {
                        "item": f"settings.wavetables.{index}",
                        "reason": "wavetable_growth_refused",
                        "detail": str(exc.message),
                    }
                )
                continue
            if data_dir_path is None or preset_id is None:
                gaps.append(
                    {
                        "item": f"settings.wavetables.{index}",
                        "reason": "wavetable_source_unavailable",
                        "detail": "the apply needs the preset's own path to carry its table",
                    }
                )
                continue
            wavetable_specs.append(
                {
                    "oscillator": index + 1,
                    "from_preset": str(data_dir_path / preset_id),
                    "preset_slot": index + 1,
                }
            )

    file_sample = file_settings.get("sample")
    if isinstance(file_sample, dict) and live_settings.get("sample") != file_sample:
        growth = codec_module._growth_of(document, codec_module.SAMPLE_PATH, file_sample)
        if growth <= codec_module.BATCH_GROWTH_BUDGET:
            paths[codec_module.SAMPLE_PATH] = file_sample
            counts["sample"] = 1
        else:
            gaps.append(
                {
                    "item": codec_module.SAMPLE_PATH,
                    "reason": "sample_growth_over_budget",
                    "detail": (
                        f"the sample payload would grow the document by {growth} characters; "
                        f"writes above the measured budget (~+40) silently do not land, so the "
                        f"sample is not carried"
                    ),
                    "growth_chars": growth,
                    "budget_chars": codec_module.BATCH_GROWTH_BUDGET,
                }
            )

    return {
        "paths": paths,
        "wavetables": wavetable_specs,
        "routes_create": route_plan["creates"],
        "routes_update": route_plan["updates"],
        "routes_remove": route_plan["removes"],
        "gaps": gaps,
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# Destinations the running plugin DROPS (P38, measured 2026-10-17)
# ---------------------------------------------------------------------------

#: The memo of dropped destinations. It lives BESIDE the catalog cache (same
#: directory, same rule: machine-local and regenerable).
DROPS_FILE = "vital_destination_drops.json"

DROPS_SCHEMA_VERSION = 1


def _drops_path(directory: Path) -> Path:
    return Path(directory) / DROPS_FILE


def read_dropped_destinations(directory: Path) -> dict[str, Any]:
    """The memo of pairs a build dropped: `{destination: row}` plus its header.

    A missing or unreadable file is NOT an error: this is an optimization, and an
    unreadable memo must degrade to "nothing known" rather than refuse an apply.
    Every row carries the build that measured it, because a NEWER plugin build may
    know the name again - a skip is only trusted for the version that measured it.
    """
    empty: dict[str, Any] = {"schema": DROPS_SCHEMA_VERSION, "destinations": {}}
    try:
        data = json.loads(_drops_path(directory).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict) or not isinstance(data.get("destinations"), dict):
        return empty
    data.setdefault("schema", DROPS_SCHEMA_VERSION)
    return data


def known_dropped_destinations(directory: Path, synth_version: str) -> list[str]:
    """Destinations measured as dropped **by this build** (empty when unknown)."""
    data = read_dropped_destinations(directory)
    version = str(synth_version or "")
    names: list[str] = []
    for name, row in sorted((data.get("destinations") or {}).items()):
        if not isinstance(row, dict):
            continue
        if version and str(row.get("synth_version") or "") == version:
            names.append(str(name))
    return names


def record_dropped_destinations(
    directory: Path,
    rows: Any,
    *,
    synth_version: str = "",
    preset_id: str = "",
    stamp: str | None = None,
) -> dict[str, Any]:
    """Merge measured drops into the memo (atomic write, same discipline as a save).

    `rows` are the route operations a transaction reported as dropped; the write is
    a temp file + `os.replace` so an interrupted apply cannot leave a half-written
    memo behind.
    """
    data = read_dropped_destinations(directory)
    table = data.setdefault("destinations", {})
    now = stamp or utc_now()
    recorded: list[str] = []
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        destination = str(row.get("destination") or "")
        if not destination:
            continue
        existing = table.get(destination) if isinstance(table.get(destination), dict) else {}
        table[destination] = {
            "destination": destination,
            "source": str(row.get("source") or ""),
            "slot": row.get("slot"),
            "synth_version": str(synth_version or ""),
            "count": int(existing.get("count") or 0) + 1,
            "first_seen": existing.get("first_seen") or now,
            "last_seen": now,
            "last_preset": str(preset_id or ""),
        }
        recorded.append(destination)
    data["schema"] = DROPS_SCHEMA_VERSION
    data["updated"] = now
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = _drops_path(directory)
    text = json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False)
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    finally:
        if temp.exists():  # pragma: no cover - the replace succeeded or we clean up
            try:
                temp.unlink()
            except OSError:
                pass
    return {"file": str(path), "recorded": recorded, "known": sorted(table)}


# ---------------------------------------------------------------------------
# Saving the live state as a preset file
# ---------------------------------------------------------------------------


def _clean_name(name: str) -> str:
    text = str(name).strip()
    if not text:
        raise InvalidValue("a preset name is required", detail={"reason": "empty_name"})
    if _UNSAFE_NAME_RE.search(text) or text in {".", ".."} or text.endswith((".", " ")):
        raise InvalidValue(
            f"preset name {name!r} contains characters a file name cannot carry",
            detail={"name": str(name), "reason": "unsafe_name"},
        )
    return text[:120]


def save_document(
    document: dict[str, Any],
    *,
    name: str,
    bank: str | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Write the live document as a NEW preset file (auto-suffixed on collision).

    The serialization mirrors the plugin's own writer (single line, keys in
    sorted order - nlohmann's `std::map` order, measured in the plugin's source),
    because a `.vital` file is read back by the plugin and its readers tolerate
    the writer's own shape best. The write is atomic (temp file + replace) so a
    failed save never leaves a half-written preset in the library.
    """
    root = Path(root) if root is not None else data_dir()
    stem = _clean_name(name)
    folder = root / "User" / "Presets"
    if bank is not None and str(bank).strip():
        folder = folder / _clean_name(bank)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{stem}{PRESET_EXT}"
    suffix = 1
    while target.exists():
        suffix += 1
        target = folder / f"{stem}_{suffix}{PRESET_EXT}"
        if suffix > 999:
            raise InvalidValue(
                "more than 999 presets share this name; refusing to guess further",
                detail={"name": stem, "folder": str(folder)},
            )
    text = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    payload = text.encode("utf-8")
    temp = folder / f".{stem}{PRESET_EXT}.tmp-{os.getpid()}"
    try:
        with open(temp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        if temp.exists():  # pragma: no cover - the replace succeeded or we clean up
            try:
                temp.unlink()
            except OSError:
                pass
    return {
        "path": str(target),
        "id": relative_id(target, root),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "suffix": suffix if suffix > 1 else 0,
        "note": (
            "the plugin's browser rescans its library on its own save/import only "
            "(no filesystem watcher, measured in its source): a new file on disk may "
            "need the browser to be reopened before it lists it"
        ),
    }


# ---------------------------------------------------------------------------
# The capability object the tool layer talks to
# ---------------------------------------------------------------------------


class PresetCapability:
    """Preset library operations for one provider - what `plugin_presets` calls.

    Every method takes/returns plain data and raises typed `PluginError`s; the
    capability object never touches the host.
    """

    def __init__(self, *, root: Path | None = None, catalog: Any = None, drops_dir: Path | None = None) -> None:
        self._root = Path(root) if root is not None else None
        self._catalog = catalog
        self._drops_dir = Path(drops_dir) if drops_dir is not None else None

    def data_dir(self) -> Path:
        if self._root is None:
            self._root = data_dir()
        return self._root

    def drops_directory(self) -> Path:
        """Where the dropped-destination memo lives (the catalog cache directory)."""
        if self._drops_dir is None:
            from . import catalog as catalog_module

            self._drops_dir = catalog_module.catalog_directory()
        return self._drops_dir

    def known_dropped_destinations(self, synth_version: str) -> list[str]:
        return known_dropped_destinations(self.drops_directory(), synth_version)

    def record_dropped_destinations(
        self, rows: Any, *, synth_version: str = "", preset_id: str = ""
    ) -> dict[str, Any]:
        return record_dropped_destinations(
            self.drops_directory(), rows, synth_version=synth_version, preset_id=preset_id
        )

    def _catalog_for(self) -> Any:
        if self._catalog is None:
            from . import catalog as catalog_module

            self._catalog = catalog_module.Catalog(self.data_dir())
        return self._catalog

    def search(
        self,
        *,
        query: str | None = None,
        style: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        return self._catalog_for().search(query=query, style=style, limit=limit, offset=offset)

    def info(self, preset_id: str) -> dict[str, Any]:
        path = safe_join(self.data_dir(), preset_id)
        meta = read_preset_metadata(path)
        payload = path.read_bytes()
        row = {
            "id": relative_id(path, self.data_dir()),
            "path": str(path),
            "name": meta["name"],
            "embedded_name": meta["embedded_name"],
            "author": meta["author"],
            "style": meta["style"],
            "synth_version": meta["synth_version"],
            "size": meta["size"],
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        try:
            preset = load_preset(path)
        except PluginError as exc:
            row["document_error"] = exc.message
            return row
        settings = preset.get("settings") or {}
        row["keys"] = {
            "settings": len(settings),
            "controls": len([key for key in settings if key not in BLOCK_KEYS]),
            "has_sample_payload": bool(isinstance(settings.get("sample"), dict) and settings["sample"].get("samples")),
        }
        row["macros"] = {key: preset.get(key) for key in ("macro1", "macro2", "macro3", "macro4")}
        return row

    def plan(
        self,
        document: dict[str, Any],
        preset_id: str,
        *,
        guarded_paths: Any = (),
        dropped_destinations: Any = (),
    ) -> dict[str, Any]:
        """The change set a `plugin_presets(op="apply")` would write (dry-run core)."""
        path = safe_join(self.data_dir(), preset_id)
        preset = load_preset(path)
        outcome = change_set(
            document,
            preset,
            data_dir_path=self.data_dir(),
            preset_id=preset_id,
            guarded_paths=guarded_paths,
            dropped_destinations=dropped_destinations,
        )
        outcome["preset"] = {
            "id": preset_id,
            "path": str(path),
            "name": path.stem,
            "embedded_name": preset.get("preset_name") or "",
            "author": preset.get("author") or "",
            "style": preset.get("preset_style") or "",
            "synth_version": preset.get("synth_version") or "",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        return outcome

    def save(
        self,
        document: dict[str, Any],
        *,
        name: str,
        bank: str | None = None,
    ) -> dict[str, Any]:
        return save_document(document, name=name, bank=bank, root=self.data_dir())
