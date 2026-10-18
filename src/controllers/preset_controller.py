"""REAPER's OWN FX preset store (the preset dropdown in the FX window) as a fast channel.

Why this exists next to `plugin_presets` (the plugin's own browser library): they are different
stores with different costs. A REAPER preset is a file REAPER itself reads (`presets/<type>-<name>.ini`
in the resource path - MEASURED 2026-10-18: `presets/vst3-Vital.ini`, 4 presets, format documented in
`utils/reaper_presets.py`), and applying one is a single host call that lets the PLUGIN load its own
state - no chunk read, no chunk write, no state-size ceiling. That makes it the fast path when the
sound already exists as a preset, and it is the path a producer uses by hand.

What the host API offers (all present in `reapy.reascript_api`, MEASURED by enumeration):
`TrackFX_GetPreset`, `TrackFX_GetPresetIndex`, `TrackFX_SetPreset`, `TrackFX_SetPresetByIndex`,
`TrackFX_NavigatePresets`, `TrackFX_GetUserPresetFilename` - and **no save call at all**. Saving is
therefore done by writing REAPER's own preset file in REAPER's own measured format, and it is
VERIFIED against the host before it is reported as done: the preset count must grow, the new name
must become selectable, and the state it applies must reproduce the bytes that were saved. Anything
else restores the file and refuses with what was measured.
"""

from __future__ import annotations

import os
import shutil
import time
from typing import Any

import reapy

from controllers.base_controller import BaseController
from utils.reaper_presets import (
    append_preset,
    extract_fx_state,
    parse_preset_library,
    preset_names,
    verify_preset_payload,
)

#: Buffer size for the host's string out-parameters (a preset name is short; the filename is a path).
_BUFFER = 1024

#: The measured read ceiling of `GetTrackStateChunk` (past it the host faults, plugin layer P40).
_CHUNK_CAPACITY = 4_100_000


class PresetController(BaseController):
    """Read, apply, navigate and save REAPER's own FX presets for one FX slot."""

    # -- plumbing -----------------------------------------------------------

    def _track(self, track_index: int):
        project = reapy.Project()
        if int(track_index) == -1:
            return project.master_track
        return project.tracks[int(track_index)]

    def _call_string(self, function, *args: Any) -> tuple[str | None, str]:
        """Call a host function that fills a char* buffer; returns (text, shape).

        reapy's generated wrappers expose an out-string either as a second tuple element
        (`(retval, buffer)`) or as the call's own result. Both shapes are tried and the one that
        produced a string is reported, so a live run says which convention this build uses instead
        of the layer pretending to know.
        """
        attempts = (
            ("tuple", lambda: function(*args, "", _BUFFER)),
            ("direct", lambda: function(*args, "")),
        )
        for shape, call in attempts:
            try:
                result = call()
            except Exception:  # noqa: BLE001 - a wrong shape is not a failure of the channel
                continue
            if isinstance(result, (list, tuple)):
                for item in result[1:]:
                    if isinstance(item, str):
                        return item.rstrip("\x00").strip(), shape
                continue
            if isinstance(result, str):
                return result.rstrip("\x00").strip(), shape
        return None, "none"

    def _preset_index(self, track_index: int, fx_index: int) -> tuple[int | None, int | None, str]:
        """`(current index, number of presets, shape)` from `TrackFX_GetPresetIndex`."""
        track = self._track(track_index)
        function = reapy.reascript_api.TrackFX_GetPresetIndex
        try:
            result = function(track.id, int(fx_index), 0)
        except Exception:  # noqa: BLE001 - reported as an unreadable channel, never as zero presets
            return None, None, "error"
        if isinstance(result, (list, tuple)):
            values = [item for item in result if isinstance(item, int)]
            if len(values) >= 2:
                return values[0], values[1], "tuple"
            if values:
                return values[0], None, "tuple"
            return None, None, "tuple"
        if isinstance(result, int):
            return result, None, "direct"
        return None, None, "none"

    def _current_preset(self, track_index: int, fx_index: int) -> tuple[str | None, str]:
        track = self._track(track_index)
        return self._call_string(reapy.reascript_api.TrackFX_GetPreset, track.id, int(fx_index))

    def _user_preset_file(self, track_index: int, fx_index: int) -> tuple[str | None, str]:
        track = self._track(track_index)
        return self._call_string(
            reapy.reascript_api.TrackFX_GetUserPresetFilename, track.id, int(fx_index)
        )

    def _read_chunk(self, track_index: int) -> str:
        """The whole track chunk, with the measured capacity (a fault is reported, not raised)."""
        track = self._track(track_index)
        result = reapy.reascript_api.GetTrackStateChunk(track.id, "", _CHUNK_CAPACITY, False)
        if isinstance(result, (list, tuple)):
            for item in result:
                if isinstance(item, str) and len(item) > 100:
                    return item
            for item in result:
                if isinstance(item, str):
                    return item
        if isinstance(result, str):
            return result
        raise RuntimeError("GetTrackStateChunk returned no text")

    # -- reading ------------------------------------------------------------

    def get_fx_presets(self, track_index: int, fx_index: int) -> dict[str, Any]:
        """What REAPER knows about this FX's presets: the current one, the count, the file, names.

        The names come from REAPER's own preset file (read-only parse, no host calls per preset),
        and the host's count is reported next to them so the two sources can be compared: a file
        REAPER has not re-read yet, or a plugin whose preset channel is empty, both show up here as
        a disagreement instead of an assumption.
        """
        name, name_shape = self._current_preset(track_index, fx_index)
        index, count, index_shape = self._preset_index(track_index, fx_index)
        path, path_shape = self._user_preset_file(track_index, fx_index)
        data: dict[str, Any] = {
            "track_index": int(track_index),
            "fx_index": int(fx_index),
            "current": name,
            "preset_index": index,
            "host_preset_count": count,
            "user_preset_file": path,
            "read_shapes": {
                "current": name_shape,
                "index": index_shape,
                "file": path_shape,
            },
        }
        file_names: list[dict[str, Any]] = []
        problems: list[dict[str, Any]] = []
        text = ""
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
                    text = handle.read()
                file_names = preset_names(text)
                problems = parse_preset_library(text)["problems"]
            except OSError as exc:
                problems = [{"reason": "unreadable_preset_file", "detail": str(exc)}]
        else:
            data["file_present"] = False
        data["presets"] = file_names
        data["file_preset_count"] = len(file_names)
        data["file_preset_index"] = None
        for row in file_names:
            if row.get("name") == name:
                data["file_preset_index"] = row.get("index")
                break
        data["problems"] = problems
        # The format's own integrity check (a checksum byte per chunk): reported for the preset
        # that is CURRENT, because that is the one whose bytes a caller may rely on right now.
        current_row = next(
            (row for row in file_names if row.get("name") == name and row.get("index") is not None),
            None,
        )
        if current_row is not None:
            check = verify_preset_payload(text, current_row["index"])
            data["current_payload"] = {
                "section": current_row.get("section"),
                "bytes": check.get("bytes"),
                "checksums_ok": check.get("checksums_ok"),
                "bad_chunks": check.get("bad_chunks"),
                "len_ok": check.get("len_ok"),
            }
        if count == 0 and not file_names:
            data["channel_empty"] = (
                "the host reports no presets and no preset file is present for this FX: the channel "
                "is empty on this build (a plugin that manages its own presets exposes none here)"
            )
        elif count is not None and file_names and count != len(file_names):
            data["disagreement"] = (
                "the host's preset count and the preset file disagree - REAPER may not have re-read "
                "the file yet; applying a preset by index uses the HOST's numbering, by name uses "
                "the file's names"
            )
        if path:
            data["file_bytes"] = os.path.getsize(path) if os.path.isfile(path) else None
        return {"status": "success", **data}

    # -- applying -----------------------------------------------------------

    def set_fx_preset(
        self, track_index: int, fx_index: int, *, name: str | None = None, index: int | None = None
    ) -> dict[str, Any]:
        """Apply one preset from REAPER's own set, by NAME or by INDEX, and verify it landed.

        Exactly one selector is required - a call naming both would have to pick one silently. The
        verification is the host's own read-back (`TrackFX_GetPreset` and the index), so a selector
        that the host did not accept is reported as not applied instead of being assumed.
        """
        has_name = name is not None and str(name).strip() != ""
        has_index = index is not None
        if has_name == has_index:
            return {
                "status": "error",
                "message": "name ONE selector: either `name` (a preset name) or `index` (its number)",
            }
        before_name, _ = self._current_preset(track_index, fx_index)
        before_index, _, _ = self._preset_index(track_index, fx_index)
        track = self._track(track_index)
        try:
            if has_index:
                applied = reapy.reascript_api.TrackFX_SetPresetByIndex(
                    track.id, int(fx_index), int(index)
                )
                selector: Any = int(index)
                how = "index"
            else:
                applied = reapy.reascript_api.TrackFX_SetPreset(
                    track.id, int(fx_index), str(name)
                )
                selector = str(name)
                how = "name"
        except Exception as exc:  # noqa: BLE001 - the host's own refusal is the message
            return {"status": "error", "message": f"the host refused the preset call: {exc}"}
        after_name, _ = self._current_preset(track_index, fx_index)
        after_index, count, _ = self._preset_index(track_index, fx_index)
        matched = (after_name == selector) if how == "name" else (after_index == selector)
        return {
            "status": "success" if applied else "error",
            "applied": bool(applied),
            "selector": {"how": how, "value": selector},
            "before": {"name": before_name, "index": before_index},
            "after": {"name": after_name, "index": after_index, "count": count},
            "verified": bool(matched),
            "message": (
                f"preset applied and read back: {after_name!r} (index {after_index})"
                if matched
                else (
                    f"the host call returned {bool(applied)} but the read-back is "
                    f"{after_name!r} (index {after_index}) - the preset did NOT become current"
                )
            ),
        }

    def navigate_fx_presets(self, track_index: int, fx_index: int, *, step: int = 1) -> dict[str, Any]:
        """Step through REAPER's presets (`TrackFX_NavigatePresets`) and report where it landed."""
        before_name, _ = self._current_preset(track_index, fx_index)
        before_index, _, _ = self._preset_index(track_index, fx_index)
        track = self._track(track_index)
        try:
            moved = reapy.reascript_api.TrackFX_NavigatePresets(
                track.id, int(fx_index), int(step)
            )
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": f"the host refused the navigation: {exc}"}
        after_name, _ = self._current_preset(track_index, fx_index)
        after_index, count, _ = self._preset_index(track_index, fx_index)
        changed = (after_index, after_name) != (before_index, before_name)
        return {
            "status": "success" if moved else "error",
            "moved": bool(moved),
            "step": int(step),
            "before": {"name": before_name, "index": before_index},
            "after": {"name": after_name, "index": after_index, "count": count},
            "changed": changed,
        }

    # -- saving -------------------------------------------------------------

    def save_fx_preset(
        self, track_index: int, fx_index: int, *, name: str, dry_run: bool = True
    ) -> dict[str, Any]:
        """Save the FX's CURRENT state as a REAPER preset, verified before it is reported.

        The API has no save call (MEASURED by enumeration of `reapy.reascript_api`), so this writes
        REAPER's own preset file in REAPER's own measured format and then makes the HOST prove it:
        the preset count must grow, the new name must become selectable, and the state that comes
        back after applying it must equal the bytes that were saved. Any step that fails restores
        the file, re-applies the preset that was current, and refuses with what was measured -
        a preset believed to be saved but not readable by REAPER would be a trap for the owner.
        """
        wanted = str(name).strip()
        if not wanted:
            return {"status": "error", "message": "a preset needs a non-empty `name`"}
        path, _shape = self._user_preset_file(track_index, fx_index)
        if not path:
            return {
                "status": "error",
                "message": "the host reports no user preset file for this FX (the channel is empty)",
            }
        try:
            state = extract_fx_state(self._read_chunk(track_index), int(fx_index))
        except Exception as exc:  # noqa: BLE001 - an unreadable state is not a savable state
            return {"status": "error", "message": f"the FX state could not be read: {exc}"}
        payload = state["data"]
        if not payload:
            return {"status": "error", "message": "the FX state is empty"}
        text = ""
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
                text = handle.read()
        try:
            appended = append_preset(text or "[General]\nNbPresets=0\n", wanted, payload)
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        before_name, _ = self._current_preset(track_index, fx_index)
        before_index, before_count, _ = self._preset_index(track_index, fx_index)
        plan = {
            "file": path,
            "section": appended["section"],
            "name": wanted,
            "payload_bytes": len(payload),
            "nb_presets_before": appended["nb_presets_before"],
            "nb_presets_after": appended["nb_presets_after"],
            "characters_before": len(text),
            "characters_after": len(appended["text"]),
            "current_preset_before": before_name,
            "host_preset_count_before": before_count,
        }
        if dry_run:
            return {"status": "success", "dry_run": True, "would_write": plan}
        backup = f"{path}.reaper-mcp-backup"
        if os.path.isfile(path) and not os.path.isfile(backup):
            shutil.copy2(path, backup)
        original = text
        # MEASURED: REAPER's own preset files are CRLF (140 CRLF, 0 lone LF in `vst3-Vital.ini`),
        # so the whole file is written back that way - a section appended with LF endings is a file
        # REAPER did not write.
        with open(path, "w", encoding="utf-8", newline="\r\n") as handle:
            handle.write(appended["text"])
        _, after_count, _ = self._preset_index(track_index, fx_index)
        grew = before_count is not None and after_count is not None and after_count > before_count
        applied = self.set_fx_preset(track_index, fx_index, name=wanted)
        applied_ok = bool(applied.get("applied"))
        back_state: bytes | None = None
        if applied_ok:
            try:
                back_state = extract_fx_state(self._read_chunk(track_index), int(fx_index))["data"]
            except Exception:  # noqa: BLE001 - the read-back below reports the failure
                back_state = None
        identical = back_state == payload
        if not (grew and applied_ok and identical):
            with open(path, "w", encoding="utf-8", newline="\r\n") as handle:
                handle.write(original)
            if before_name:
                self.set_fx_preset(track_index, fx_index, name=before_name)
            return {
                "status": "error",
                "message": (
                    "the saved preset was not confirmed by the host, so the file was restored: "
                    f"count {before_count} -> {after_count} (must grow), selected {applied_ok}, "
                    f"state identical {identical}"
                ),
                "restored": True,
                "plan": plan,
                "verification": {
                    "host_preset_count_after": after_count,
                    "applied": applied_ok,
                    "state_identical": identical,
                    "backup": backup if os.path.isfile(backup) else None,
                },
            }
        if before_name:
            self.set_fx_preset(track_index, fx_index, name=before_name)
        return {
            "status": "success",
            "saved": plan,
            "verification": {
                "host_preset_count_after": after_count,
                "applied": applied_ok,
                "state_identical": identical,
                "payload_bytes": len(payload),
                "backup": backup if os.path.isfile(backup) else None,
                "restored_current": before_name,
            },
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }


__all__ = ["PresetController"]
