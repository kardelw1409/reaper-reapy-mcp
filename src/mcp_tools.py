from mcp import types
from mcp.server.fastmcp import FastMCP, Context
from typing import Optional, Dict, Any, List, Union
import math
import reapy
from utils.position_utils import position_to_time, time_to_measure, get_time_map_info, measure_length_to_time


# ---------------------------------------------------------------------------
# MIDI note geometry (P36): resolve + validate BEFORE anything is written.
#
# The host stores a note whose end lies past the item end but its own read path
# reports it `is_visible: false` - it never sounds (measured P34/P36: one 0.6 s
# note ended 0.26 s past the item and stayed silent until the item was
# lengthened). The MCP layer therefore mirrors the controller's own visibility
# rule (`0 <= relative_start < item_length` and `0 < relative_end <=
# item_length`, midi_controller.py) on the INPUT values, per note, and refuses
# the whole request before the first write. Nothing is clipped, snapped or
# moved: an invalid request changes nothing and says exactly why.
# ---------------------------------------------------------------------------

_MIDI_PITCH_MIN, _MIDI_PITCH_MAX = 0, 127
_MIDI_VELOCITY_MIN, _MIDI_VELOCITY_MAX = 0, 127


def _midi_int(value: Any, low: int, high: int, where: str):
    """(int, error) for an integer parameter constrained to ``low..high``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, f"{where} must be an integer in {low}..{high} (got {value!r})"
    number = float(value)
    if not math.isfinite(number) or number != int(number) or not (low <= number <= high):
        return None, f"{where} must be an integer in {low}..{high} (got {value!r})"
    return int(number), None


def _normalize_length_measure(length_measure: str):
    """The historical ``M:0,0`` rewrite; returns ``(normalized, error)``."""
    lm = str(length_measure).strip()
    if lm in ("0:0,0", "0:0.0"):
        return None, "Invalid length_measure: zero length specified (0:0,0)"
    if ":0,0" in lm or ":0.0" in lm:
        try:
            measure_part = int(lm.split(":")[0])
        except Exception:
            return None, f"Invalid length_measure format: {length_measure}"
        return f"{measure_part}:1,0", None
    return lm, None


def _resolve_note(item_start: float, item_length: float, note: Any,
                  relative_default: bool, where: str):
    """Resolve one note request to (geometry, error); ``error`` is a message.

    The geometry carries the absolute start/end in seconds and the item-relative
    start/end - the values the write path uses. Validation is the host read
    path's own visibility rule, applied to the input values: a note it would
    hide is refused here, with its numbers, instead of being written.
    """
    if not isinstance(note, dict):
        return None, f"{where} must be an object with pitch/start/length fields (got {type(note).__name__})"

    pitch, error = _midi_int(note.get("pitch"), _MIDI_PITCH_MIN, _MIDI_PITCH_MAX, f"{where}.pitch")
    if error:
        return None, error
    velocity, error = _midi_int(note.get("velocity", 96), _MIDI_VELOCITY_MIN,
                                _MIDI_VELOCITY_MAX, f"{where}.velocity")
    if error:
        return None, error

    relative = note.get("relative_start", relative_default)
    if not isinstance(relative, bool):
        return None, f"{where}.relative_start must be true or false (got {relative!r})"

    start_time = note.get("start_time")
    start_measure = note.get("start_measure")
    if start_time is not None and start_measure is not None:
        return None, f"{where} names both start_time and start_measure; name exactly one"
    if start_time is not None:
        try:
            offset_or_abs = float(start_time)
        except Exception:
            return None, f"{where}.start_time must be a number (got {start_time!r})"
        if not math.isfinite(offset_or_abs):
            return None, f"{where}.start_time must be finite (got {start_time!r})"
        abs_time = item_start + offset_or_abs if relative else offset_or_abs
    elif start_measure is not None:
        try:
            if relative:
                offset_or_abs = measure_length_to_time(str(start_measure), item_start)
                abs_time = item_start + offset_or_abs
            else:
                abs_time = position_to_time(str(start_measure))
                offset_or_abs = abs_time
        except Exception as exc:
            return None, f"{where}.start_measure is invalid ({exc})"
        if not math.isfinite(float(abs_time)):
            return None, f"{where}.start_measure is invalid (not finite)"
    else:
        return None, f"{where} names neither start_time nor start_measure"

    length_time = note.get("length_time")
    length_measure = note.get("length_measure")
    if length_time is not None and length_measure is not None:
        return None, f"{where} names both length_time and length_measure; name exactly one"
    if length_time is not None:
        try:
            length = float(length_time)
        except Exception:
            return None, f"{where}.length_time must be a number (got {length_time!r})"
        if not math.isfinite(length):
            return None, f"{where}.length_time must be finite (got {length_time!r})"
        if length <= 0:
            return None, f"{where}.length_time must be greater than zero (got {length_time!r})"
    elif length_measure is not None:
        normalized, error = _normalize_length_measure(length_measure)
        if error:
            return None, f"{where}: {error}"
        try:
            length = measure_length_to_time(normalized, abs_time)
        except Exception as exc:
            return None, f"{where}.length_measure is invalid ({exc})"
        if length <= 0:
            # the converter clamps a non-positive computed duration to 0.1 s, so a
            # value that reaches this branch states a zero/negative span itself
            return None, f"{where}.length_measure resolves to no duration (got {length_measure!r})"
    else:
        return None, f"{where} names neither length_time nor length_measure"

    rel_start = abs_time - item_start
    rel_end = rel_start + length
    if rel_start < 0:
        return None, (
            f"{where} starts {abs(-rel_start):.9g} s before the MIDI item start; "
            "a note outside the item does not sound and was not added"
        )
    if rel_end > item_length:
        return None, (
            f"{where} ends {rel_end - item_length:.9g} s past the MIDI item end "
            f"(item is {item_length:.9g} s long; the note would span {rel_start:.9g}..{rel_end:.9g} s "
            "inside it and would not sound); nothing was added. Shorten the note or make the item longer."
        )
    return {
        "pitch": pitch,
        "velocity": velocity,
        "relative_start": relative,
        "abs_start": abs_time,
        "abs_end": abs_time + length,
        "rel_start": rel_start,
        "rel_end": rel_end,
        "length": length,
    }, None


def setup_mcp_tools(mcp: FastMCP, controller) -> None:
    """Setup MCP tools for Reaper control."""
    
    @mcp.tool("test_connection")
    def test_connection(ctx: Context) -> Dict[str, Any]:
        """Test connection to Reaper."""
        try:
            if controller.verify_connection():
                return {"status": "success", "message": "Connected to Reaper"}
            return {"status": "error", "message": "Failed to connect to Reaper"}
        except Exception as e:
            return {"status": "error", "message": f"Connection test failed: {str(e)}"}

    @mcp.tool("get_region_list")
    def get_region_list(ctx: Context) -> Dict[str, Any]:
        """Get a list of all regions in the project.

        Returns:
            Dict containing status and a list of regions, each with:
            - index: region index
            - start: region start position
            - end: region end position
        """
        try:
            regions = []
            project = reapy.Project()

            for i, region in enumerate(project.regions):
                region_info = {
                    "index": i,
                    "start": region.start,
                    "end": region.end
                }
                regions.append(region_info)
            return {
                "status": "success",
                "regions": regions,
                "count": len(regions)
            }
        except Exception as e:
            return {"status": "error", "message": f"Failed to get region list: {str(e)}"}


    @mcp.tool("create_track")
    def create_track(ctx: Context, name: Optional[str] = None) -> Dict[str, Any]:
        """Note: Track indices in REAPER API are 0-based (first track = 0, second = 1, etc.)
              However, REAPER's visual interface displays tracks starting from 1.
              So track 0 in API = Track 1 in REAPER UI.
        """
        try:
            track_index = controller.create_track(name)
            return {
                "status": "success",
                "track_index": track_index,
                "message": f"Created track {track_index}",
            }
        except Exception as e:
            return {"status": "error", "message": f"Failed to create track: {str(e)}"}

    @mcp.tool("rename_track")
    def rename_track(ctx: Context, track_index: int, new_name: str) -> Dict[str, Any]:
        """Rename an existing track.
        
        Args:
            track_index: 0-based track index (0 = first track, 1 = second, etc.)
                        Note: REAPER UI shows tracks starting from 1, but API uses 0-based indexing.
                        API track 0 = UI Track 1, API track 1 = UI Track 2, etc.
            new_name: New name for the track
        """
        try:
            if controller.rename_track(track_index, new_name):
                return {"status": "success", "message": f"Renamed track {track_index} to {new_name}"}
            return {"status": "error", "message": f"Failed to rename track {track_index}"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to rename track: {str(e)}"}

    @mcp.tool("move_track")
    def move_track(ctx: Context, track_index: int, new_index: int) -> Dict[str, Any]:
        """Move a track to a new index.

        Args:
            track_index: Current track index (0-based)
            new_index: Desired track index after move (0-based)
        """
        try:
            if controller.move_track(track_index, new_index):
                return {"status": "success", "message": f"Moved track {track_index} to index {new_index}"}
            return {"status": "error", "message": "Failed to move track"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to move track: {str(e)}"}

    @mcp.tool("set_track_volume")
    def set_track_volume(ctx: Context, track_index: int, volume: float) -> Dict[str, Any]:
        """Set the volume (fader) for a track.

        Args:
            track_index: Track index
            volume: Volume level (0.0 to 1.0, linear)
        """
        try:
            if controller.set_track_volume(track_index, volume):
                return {"status": "success", "message": f"Set track {track_index} volume to {volume}"}
            return {"status": "error", "message": "Failed to set track volume"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to set track volume: {str(e)}"}

    @mcp.tool("get_track_volume")
    def get_track_volume(ctx: Context, track_index: int) -> Dict[str, Any]:
        """Get the volume (fader) for a track.

        Args:
            track_index: Track index
        """
        try:
            volume = controller.get_track_volume(track_index)
            return {"status": "success", "volume": volume}
        except Exception as e:
            return {"status": "error", "message": f"Failed to get track volume: {str(e)}"}

    @mcp.tool("create_track_send")
    def create_track_send(
        ctx: Context,
        source_track_index: int,
        destination_track_index: int,
        volume: float = 1.0,
    ) -> Dict[str, Any]:
        """Create a track send and set its volume.

        Args:
            source_track_index: Source track index
            destination_track_index: Destination track index
            volume: Send volume (linear, 1.0 = 0 dB)
        """
        try:
            send_idx = controller.create_track_send(source_track_index, destination_track_index, volume)
            if send_idx is not None:
                return {
                    "status": "success",
                    "message": f"Created send {send_idx} from track {source_track_index} to {destination_track_index} at volume {volume}",
                    "send_index": send_idx,
                }
            return {"status": "error", "message": "Failed to create track send"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to create track send: {str(e)}"}

    @mcp.tool("set_tempo")
    def set_tempo(ctx: Context, bpm: float) -> Dict[str, Any]:
        """Set the project tempo."""
        try:
            if controller.set_tempo(bpm):
                return {"status": "success", "message": f"Set tempo to {bpm} BPM"}
            return {"status": "error", "message": "Failed to set tempo"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to set tempo: {str(e)}"}

    @mcp.tool("get_tempo")
    def get_tempo(ctx: Context) -> Dict[str, Any]:
        """Get the current project tempo."""
        try:
            tempo = controller.get_tempo()
            return {"status": "success", "message": f"Current tempo: {tempo} BPM"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to get tempo: {str(e)}"}

    @mcp.tool("set_track_color")
    def set_track_color(ctx: Context, track_index: int, color: str) -> Dict[str, Any]:
        """Set the color of a track.
        
        Args:
            track_index: 0-based track index (0 = first track, 1 = second track, etc.)
            color: Color value
        """
        try:
            if controller.set_track_color(track_index, color):
                return {"status": "success", "message": f"Set color of track {track_index} to {color}"}
            return {"status": "error", "message": f"Failed to set color for track {track_index}"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to set track color: {str(e)}"}

    @mcp.tool("get_track_color")
    def get_track_color(ctx: Context, track_index: int) -> Dict[str, Any]:
        """Get the color of a track.
        
        Args:
            track_index: 0-based track index (0 = first track, 1 = second track, etc.)
        """
        try:
            color = controller.get_track_color(track_index)
            return {"status": "success", "message": f"Color of track {track_index}: {color}"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to get track color: {str(e)}"}

    @mcp.tool("add_fx")
    def add_fx(ctx: Context, track_index: int, fx_name: str) -> Dict[str, Any]:
        """Add an FX to a track.

        The controller answers a bool, so this tool must not treat its answer as an
        index: a failed add is an error envelope, and on success the index of the
        added FX is **re-read** from the track (the FX is appended last) instead of
        being guessed - the tool previously answered "success" even when the add
        failed, because ``False >= 0`` is true in Python.
        """
        try:
            added = controller.add_fx(track_index, fx_name)
            if added:
                fx_list = controller.get_fx_list(track_index) or []
                fx_index = len(fx_list) - 1 if fx_list else None
                where = f" at index {fx_index}" if fx_index is not None else ""
                return {
                    "status": "success",
                    "track_index": track_index,
                    "fx_index": fx_index,
                    "message": f"Added FX {fx_name} to track {track_index}{where}",
                }
            return {"status": "error", "message": f"Failed to add FX to track {track_index}"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to add FX: {str(e)}"}

    @mcp.tool("remove_fx")
    def remove_fx(ctx: Context, track_index: int, fx_index: int) -> Dict[str, Any]:
        """Remove an FX from a track."""
        try:
            if controller.remove_fx(track_index, fx_index):
                return {"status": "success", "message": f"Removed FX {fx_index} from track {track_index}"}
            return {"status": "error", "message": f"Failed to remove FX from track {track_index}"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to remove FX: {str(e)}"}

    @mcp.tool("set_fx_param")
    def set_fx_param(ctx: Context, track_index: int, fx_index: int, param_name: str, value: float) -> Dict[str, Any]:
        """Set ONE FX parameter value (a single, precise tweak).

        For changes to SEVERAL parameters of the same FX instance prefer the batch
        tool `plugin_write`: it takes all controls in one request, verifies the
        read-back and restores on failure (dry-run by default; a plugin without a
        prepared profile needs `plugin_prepare` first). Use this tool when you
        change exactly one value, when you are testing a single hypothesis, or
        when the plugin layer does not cover the FX.

        Values are in the host's NORMALIZED space (0.0..1.0), not display units,
        and the name is matched case-insensitively: exact name first, then the
        first substring match, then a numeric string as a parameter index - so an
        ambiguous fragment can select a different parameter than intended. When
        unsure of a name, list parameters with `get_fx_param_list` (one FX) or
        `plugin_find` (prepared instance).
        """
        try:
            if controller.set_fx_param(track_index, fx_index, param_name, value):
                return {"status": "success", "message": f"Set parameter {param_name} to {value} for FX {fx_index} on track {track_index}"}
            return {"status": "error", "message": "Failed to set FX parameter"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to set FX parameter: {str(e)}"}

    @mcp.tool("load_sampler_sample")
    def load_sampler_sample(
        ctx: Context,
        track_index: int,
        fx_index: int,
        file_path: str,
        slot: int = 0,
        param_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Load a sample file into a sampler FX (e.g., ReaSamplOmatic5000).

        Args:
            track_index: Index of the track
            fx_index: Index of the FX
            file_path: Path to the sample file
            slot: Sample slot index (used for FILE{slot} fallback)
            param_name: Explicit named config param to set (e.g., "FILE", "FILE0")
        """
        try:
            if controller.load_sampler_sample(track_index, fx_index, file_path, slot=slot, param_name=param_name):
                used = param_name or f"FILE/FILE{int(slot)}"
                return {
                    "status": "success",
                    "message": f"Loaded sample into FX {fx_index} on track {track_index} (param {used})",
                }
            return {"status": "error", "message": "Failed to load sample into sampler FX"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to load sampler sample: {str(e)}"}

    @mcp.tool("get_fx_param")
    def get_fx_param(ctx: Context, track_index: int, fx_index: int, param_name: str) -> Dict[str, Any]:
        """Get ONE FX parameter value.

        To READ many values of the same FX, prefer `get_fx_param_list` (every
        parameter of one FX in one response) or, for a prepared plugin instance,
        `plugin_read` (named controls, batched). Use this tool when you need a
        single value or are probing one parameter.
        """
        try:
            value = controller.get_fx_param(track_index, fx_index, param_name)
            return {"status": "success", "message": f"Parameter {param_name} value: {value}"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to get FX parameter: {str(e)}"}
            
    @mcp.tool("get_fx_param_list")
    def get_fx_param_list(
        ctx: Context,
        track_index: int,
        fx_index: int,
        include_values: bool = True,
        max_params: int = 0,
        start_param: int = 0,
    ) -> Dict[str, Any]:
        """List ALL parameters of one FX (the batched read for one FX).

        include_values=True (default) reads every parameter's normalized value
        and formatted text in one response: use this instead of calling
        get_fx_param once per parameter. On plugins with hundreds of parameters
        this is one host access per parameter and can be slow - for repeated work
        on such a plugin prefer `plugin_prepare` + `plugin_find` + `plugin_read`,
        which serve a prepared profile and read only the named controls.
        `max_params`/`start_param` page the list (0 = all).
        """
        try:
            param_list = controller.get_fx_param_list(
                track_index,
                fx_index,
                include_values=include_values,
                max_params=max_params,
                start_param=start_param,
            )
            if param_list:
                return {"status": "success", "message": f"Retrieved {len(param_list)} parameters", "parameters": param_list}
            return {"status": "error", "message": f"Failed to get parameters for FX {fx_index} on track {track_index}"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to get FX parameter list: {str(e)}"}
            
    @mcp.tool("get_fx_list")
    def get_fx_list(ctx: Context, track_index: int) -> Dict[str, Any]:
        """Get a list of all FX on a track."""
        try:
            fx_list = controller.get_fx_list(track_index)
            # If controller returns None -> treat as failure.
            if fx_list is None:
                return {"status": "error", "message": f"Failed to get FX list for track {track_index}"}
            # Empty list is a successful response with count 0.
            return {
                "status": "success",
                "message": f"Retrieved {len(fx_list)} FX on track {track_index}",
                "fx_list": fx_list,
                "count": len(fx_list)
            }
        except Exception as e:
            return {"status": "error", "message": f"Failed to get FX list: {str(e)}"}
            
    @mcp.tool("get_available_fx_list")
    def get_available_fx_list(ctx: Context) -> Dict[str, Any]:
        """Get a list of all available FX plugins in Reaper."""
        try:
            fx_list = controller.get_available_fx_list()
            if fx_list:
                return {"status": "success", "message": f"Retrieved {len(fx_list)} available FX plugins", "fx_list": fx_list}
            return {"status": "error", "message": "Failed to get available FX list"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to get available FX list: {str(e)}"}

    @mcp.tool("toggle_fx")
    def toggle_fx(ctx: Context, track_index: int, fx_index: int, enable: Optional[bool] = None) -> Dict[str, Any]:
        """Toggle or set the enable/disable state of an FX."""
        try:
            if controller.toggle_fx(track_index, fx_index, enable):
                state = "enabled" if enable else "disabled" if enable is not None else "toggled"
                return {"status": "success", "message": f"{state} FX {fx_index} on track {track_index}"}
            return {"status": "error", "message": "Failed to toggle FX"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to toggle FX: {str(e)}"}

    @mcp.tool("add_fx_param_automation_item")
    def add_fx_param_automation_item(
        ctx: Context,
        track_index: int,
        fx_index: int,
        param_name: str,
        start_time: Optional[float] = None,
        start_measure: Optional[str] = None,
        length_time: Optional[float] = None,
        length_measure: Optional[str] = None,
        pool_id: int = -1,
        points: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Create an automation item for an FX parameter envelope.

        Args:
            track_index: Track index
            fx_index: FX index
            param_name: FX parameter name (case-insensitive)
            start_time: Start position in seconds (optional if start_measure is provided)
            start_measure: Start position as "measure:beat,fraction" (optional if start_time is provided)
            length_time: Length in seconds (optional if length_measure is provided)
            length_measure: Length as "measures:beat,fraction" (optional if length_time is provided)
            pool_id: Automation item pool ID (-1 creates a new pool)
            points: Optional list of points. Each point supports:
                - time OR measure (absolute), OR time_offset OR measure_offset (relative to start)
                - value (normalized 0..1)
                - shape (int), tension (float), selected (bool)
        """
        try:
            if start_time is not None:
                time_start = float(start_time)
                measure_start = time_to_measure(time_start)
            elif start_measure is not None:
                time_start = position_to_time(start_measure)
                measure_start = start_measure
            else:
                return {"status": "error", "message": "Either start_time or start_measure must be provided"}

            if length_time is not None:
                length = float(length_time)
            elif length_measure is not None:
                length = measure_length_to_time(length_measure, time_start)
            else:
                return {"status": "error", "message": "Either length_time or length_measure must be provided"}

            if length <= 0:
                return {"status": "error", "message": "Length must be greater than 0"}

            resolved_points = []
            if points:
                for point in points:
                    if "value" not in point:
                        return {"status": "error", "message": "Each point must include a value"}
                    if "time" in point:
                        time_pos = float(point["time"])
                    elif "measure" in point:
                        time_pos = position_to_time(point["measure"])
                    elif "time_offset" in point:
                        time_pos = time_start + float(point["time_offset"])
                    elif "measure_offset" in point:
                        time_pos = time_start + measure_length_to_time(point["measure_offset"], time_start)
                    else:
                        return {"status": "error", "message": "Each point must include time/measure or time_offset/measure_offset"}

                    resolved_points.append({
                        "time": time_pos,
                        "value": float(point["value"]),
                        "shape": int(point.get("shape", 0)),
                        "tension": float(point.get("tension", 0.0)),
                        "selected": bool(point.get("selected", False)),
                    })

            item_idx = controller.create_fx_param_automation_item(
                track_index=track_index,
                fx_index=fx_index,
                param_name=param_name,
                start_time=time_start,
                length_time=length,
                pool_id=pool_id,
                points=resolved_points or None,
            )
            if item_idx is not None:
                return {
                    "status": "success",
                    "message": f"Created automation item {item_idx} for FX param '{param_name}'",
                    "automation_item_index": item_idx,
                    "range": {
                        "start": {"time": time_start, "measure": measure_start},
                        "end": {"time": time_start + length, "measure": time_to_measure(time_start + length)}
                    }
                }
            return {"status": "error", "message": "Failed to create automation item"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to create automation item: {str(e)}"}

    @mcp.tool("create_region")
    def create_region(ctx: Context, name: str,
                     start_time: Optional[float] = None, end_time: Optional[float] = None,
                     start_measure: Optional[str] = None, end_measure: Optional[str] = None) -> Dict[str, Any]:
        """Create a region in the project.
        
        Args:
            name: Name of the region
            start_time: Start position in seconds (optional if start_measure is provided)
            end_time: End position in seconds (optional if end_measure is provided)
            start_measure: Start position as "measure:beat,fraction" (optional if start_time is provided)
            end_measure: End position as "measure:beat,fraction" (optional if end_time is provided)
        """
        try:
            # Determine start position
            if start_time is not None:
                time_start = float(start_time)
                measure_start = time_to_measure(time_start)
            elif start_measure is not None:
                time_start = position_to_time(start_measure)
                measure_start = start_measure
            else:
                return {"status": "error", "message": "Either start_time or start_measure must be provided"}
                
            # Determine end position
            if end_time is not None:
                time_end = float(end_time)
                measure_end = time_to_measure(time_end)
            elif end_measure is not None:
                time_end = position_to_time(end_measure)
                measure_end = end_measure
            else:
                return {"status": "error", "message": "Either end_time or end_measure must be provided"}
                
            region_index = controller.create_region(time_start, time_end, name)
            if region_index >= 0:
                return {
                    "status": "success", 
                    "message": f"Created region {region_index}: {name}",
                    "range": {
                        "start": {"time": time_start, "measure": measure_start},
                        "end": {"time": time_end, "measure": measure_end}
                    }
                }
            return {"status": "error", "message": "Failed to create region"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to create region: {str(e)}"}

    @mcp.tool("delete_region")
    def delete_region(ctx: Context, region_index: int) -> Dict[str, Any]:
        """Delete a region from the project."""
        try:
            if controller.delete_region(region_index):
                return {"status": "success", "message": f"Deleted region {region_index}"}
            return {"status": "error", "message": f"Failed to delete region {region_index}"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to delete region: {str(e)}"}

    @mcp.tool("create_marker")
    def create_marker(ctx: Context, time: float, name: str) -> Dict[str, Any]:
        """Create a marker in the project."""
        try:
            marker_index = controller.create_marker(time, name)
            if marker_index >= 0:
                return {"status": "success", "message": f"Created marker {marker_index}: {name}"}
            return {"status": "error", "message": "Failed to create marker"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to create marker: {str(e)}"}

    @mcp.tool("delete_marker")
    def delete_marker(ctx: Context, marker_index: int) -> Dict[str, Any]:
        """Delete a marker from the project."""
        try:
            if controller.delete_marker(marker_index):
                return {"status": "success", "message": f"Deleted marker {marker_index}"}
            return {"status": "error", "message": f"Failed to delete marker {marker_index}"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to delete marker: {str(e)}"}

    @mcp.tool("get_master_track")
    def get_master_track(ctx: Context) -> Dict[str, Any]:
        """Get information about the master track."""
        try:
            info = controller.get_master_track()
            return {"status": "success", "message": f"Master track info: {info}"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to get master track info: {str(e)}"}

    @mcp.tool("set_master_volume")
    def set_master_volume(ctx: Context, volume: float) -> Dict[str, Any]:
        """Set the master track volume."""
        try:
            if controller.set_master_volume(volume):
                return {"status": "success", "message": f"Set master volume to {volume}"}
            return {"status": "error", "message": "Failed to set master volume"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to set master volume: {str(e)}"}

    @mcp.tool("set_master_pan")
    def set_master_pan(ctx: Context, pan: float) -> Dict[str, Any]:
        """Set the master track pan."""
        try:
            if controller.set_master_pan(pan):
                return {"status": "success", "message": f"Set master pan to {pan}"}
            return {"status": "error", "message": "Failed to set master pan"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to set master pan: {str(e)}"}

    @mcp.tool("add_master_fx")
    def add_master_fx(ctx: Context, fx_name: str) -> Dict[str, Any]:
        """Add an FX to the master track."""
        try:
            if controller.add_master_fx(fx_name):
                return {"status": "success", "message": f"Added master FX {fx_name}"}
            return {"status": "error", "message": "Failed to add master FX"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to add master FX: {str(e)}"}

    @mcp.tool("get_master_fx_list")
    def get_master_fx_list(ctx: Context) -> Dict[str, Any]:
        """Get a list of all FX on the master track."""
        try:
            fx_list = controller.get_master_fx_list()
            return {
                "status": "success",
                "message": f"Retrieved {len(fx_list)} master FX",
                "fx_list": fx_list,
                "count": len(fx_list),
            }
        except Exception as e:
            return {"status": "error", "message": f"Failed to get master FX list: {str(e)}"}

    @mcp.tool("toggle_master_mute")
    def toggle_master_mute(ctx: Context, mute: Optional[bool] = None) -> Dict[str, Any]:
        """Toggle or set the master track mute state."""
        try:
            if controller.toggle_master_mute(mute):
                state = "muted" if mute else "unmuted" if mute is not None else "toggled"
                return {"status": "success", "message": f"Master track {state}"}
            return {"status": "error", "message": "Failed to toggle master mute"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to toggle master mute: {str(e)}"}

    @mcp.tool("toggle_master_solo")
    def toggle_master_solo(ctx: Context, solo: Optional[bool] = None) -> Dict[str, Any]:
        """Toggle or set the master track solo state."""
        try:
            if controller.toggle_master_solo(solo):
                state = "soloed" if solo else "unsoloed" if solo is not None else "toggled"
                return {"status": "success", "message": f"Master track {state}"}
            return {"status": "error", "message": "Failed to toggle master solo"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to toggle master solo: {str(e)}"}
            
    @mcp.tool("get_track_count")
    def get_track_count(ctx: Context) -> Dict[str, Any]:
        """Get the number of tracks in the project."""
        try:
            count = controller.get_track_count()
            return {"status": "success", "track_count": count}
        except Exception as e:
            return {"status": "error", "message": f"Failed to get track count: {str(e)}"}
            
    @mcp.tool("get_track_list")
    def get_track_list(ctx: Context) -> Dict[str, Any]:
        """Get a list of all tracks in the project.

        Returns:
            Dict containing status and a list of tracks, each with:
            - index: Track index
            - name: Track name
            - color: Track color (if available)
        """
        try:
            tracks = []
            project = reapy.Project()
            for i, track in enumerate(project.tracks):
                track_info = {
                    "index": i,
                    "name": track.name,
                    "color": getattr(track, "color", None)
                }
                tracks.append(track_info)
            return {
                "status": "success",
                "tracks": tracks,
                "count": len(tracks)
            }
        except Exception as e:
            return {"status": "error", "message": f"Failed to get track list: {str(e)}"}

    @mcp.tool("set_track_folder")
    def set_track_folder(
        ctx: Context,
        parent_track_index: int,
        first_child_index: int,
        last_child_index: int,
        compact: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Set a folder (subgroup) in REAPER using a parent track and child range.

        Args:
            parent_track_index: Track index of the folder parent
            first_child_index: First child track index
            last_child_index: Last child track index (closes the folder)
            compact: Optional folder compact mode (0=normal, 1=small, 2=tiny)
        """
        try:
            if controller.set_track_folder(parent_track_index, first_child_index, last_child_index, compact):
                return {
                    "status": "success",
                    "message": f"Set folder on track {parent_track_index} for children {first_child_index}-{last_child_index}",
                }
            return {"status": "error", "message": "Failed to set track folder"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to set track folder: {str(e)}"}
            
    # ----- MIDI Operations -----
    
    @mcp.tool("create_midi_item")
    def create_midi_item(ctx: Context, track_index: int, 
                        start_time: Optional[float] = None, 
                        start_measure: Optional[str] = None,
                        length_time: Optional[float] = None,
                        length_measure: Optional[str] = None) -> Dict[str, Any]:
        """Create an empty MIDI item on a track. 
        
        Important: It is recommended to avoid overlapping MIDI items. Ensure to double-check before creating a new one.

        Returns both track_pos_idx and direct_item_id for flexible item referencing:
        - track_pos_idx: 0-based position index within the track (0, 1, 2, etc.)
        - direct_item_id: REAPER's internal item ID (string pointer)
        
        Both can be used interchangeably in subsequent operations like add_midi_note.
        
        Args:
            track_index: 0-based track index (0 = first track, 1 = second, etc.)
                        Note: REAPER UI shows "Track 1, Track 2" but API uses 0, 1, etc.
            start_time: Start position in seconds (optional if start_measure is provided)
            start_measure: Start position as "measure:beat,fraction" (optional if start_time is provided)
                         where fraction is milliseconds (e.g., "1:1,500" = measure 1, beat 1, half beat)
            length_time: Duration in seconds from start position (from start_time or start_measure)(optional if length_measure is provided).
            length_measure: Duration as "measure:beat,fraction" from start position (e.g., "2:1,000" = 2 measures from start_time or start_measure)(optional if length_time is provided). Also remember 2:0,0 is not end of 2 measure and 3:1,0 should be.

        To fill the created item, send the WHOLE note list in ONE `add_midi_notes`
        call (every note of the pattern is validated against the item bounds
        before anything is written); for a part that already exists as a .mid
        file - or that a MIDI-writing tool can generate (`mcp-midi-writer ::
        generate_midi`, when available) - use `insert_midi_item` instead.
        """
        try:
            # Normalize length_measure: reject explicit zero-length "0:0,0", convert "M:0,0" -> "M+1:1,0"
            if length_measure:
                lm = length_measure.strip()
                if lm in ("0:0,0", "0:0.0"):
                    return {"status": "error", "message": "Invalid length_measure: zero length specified (0:0,0)"}
                if ":0,0" in lm or ":0.0" in lm:
                    try:
                        measure_part = int(lm.split(':')[0])
                        length_measure = f"{measure_part}:1,0"
                    except Exception:
                        return {"status": "error", "message": f"Invalid length_measure format: {length_measure}"}
                        
            # Determine the time position
            if start_time is not None:
                time_pos = float(start_time)
                measure_pos = time_to_measure(time_pos)
            elif start_measure is not None:
                time_pos = position_to_time(start_measure)
                measure_pos = start_measure
            else:
                return {"status": "error", "message": "Either start_time or start_measure must be provided"}
                
            # Determine length
            if length_time is not None:
                try:
                    length_val = float(length_time)
                except Exception:
                    return {"status": "error", "message": "Invalid length_time value"}
                if length_val <= 0:
                    return {"status": "error", "message": "Note length must be greater than zero"}
                length = length_val
                end_time = time_pos + length
                length_pos = time_to_measure(end_time)
            elif length_measure is not None:
                try:
                    length = measure_length_to_time(length_measure, time_pos)
                except Exception as e:
                    return {"status": "error", "message": f"Invalid length_measure: {e}"}
                if length <= 0:
                    return {"status": "error", "message": "Note length must be greater than zero"}
                end_time = time_pos + length
                length_pos = time_to_measure(end_time)
            else:
                return {"status": "error", "message": "Either length_time or length_measure must be provided"}

            result = controller.create_midi_item(track_index, time_pos, length)
            if result['track_pos_idx'] >= 0 or result['direct_item_id']:
                return {
                    "status": "success",
                    "message": f"Created MIDI item at position {measure_pos} with length {length_pos}",
                    "track_pos_idx": result['track_pos_idx'],
                    "direct_item_id": result['direct_item_id'],
                    "position": {"time": time_pos, "measure": measure_pos},
                    "end": {"time": end_time, "measure": length_pos}
                }
            return {"status": "error", "message": "Failed to create MIDI item"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to create MIDI item: {str(e)}"}
    
    @mcp.tool("add_midi_note")
    def add_midi_note(ctx: Context, track_index: int, item: Union[int, str], pitch: int, 
                     start_time: Optional[float] = None, 
                     start_measure: Optional[str] = None,
                     length_time: Optional[float] = None,
                     length_measure: Optional[str] = None,
                     velocity: int = 96,
                     relative_start: bool = False) -> Dict[str, Any]:
        """Add ONE MIDI note to a MIDI item (single-note correction).

        For a NEW melody, bass line or drum pattern do NOT call this tool once
        per note: `add_midi_notes` takes the whole list in one call. For a
        generated, many-note or algorithmic part prefer the file path: a .mid
        written by a MIDI-writing server (`mcp-midi-writer :: generate_midi`,
        when available) inserted with `insert_midi_item` costs two calls total
        and the note list never has to be written out. Use this tool for a
        single edit of an existing part (one added or corrected note).

        The note is validated BEFORE anything is written; an invalid request
        changes nothing and says exactly why:
        - the note start must lie inside the item (relative start >= 0);
        - the note end must not pass the item end (ending exactly AT the item end
          is accepted);
        - the length must be greater than zero;
        - pitch and velocity must be integers in 0..127.
        A note ending past the item is stored by the host but never sounds, so it
        is refused instead of silently written. Nothing is clipped, snapped or
        moved, and the item is never resized.

        Positions: start_time/start_measure are ABSOLUTE project positions by
        default ("2:1,000" = measure 2, beat 1, 0 ms; fraction is milliseconds).
        With relative_start=True, start_time is a seconds offset from the item's
        start, while start_measure is measure arithmetic counted from the measure
        CONTAINING the item start (e.g. "0:1,000" = one beat after the start of
        that measure - not a fixed number of seconds), and a measure length that
        resolves below 0.1 s is floored to 0.1 s by the measure converter.
        length_time is a duration in seconds; length_measure is a duration as
        "measures:beat,fraction" (e.g. "0:2,000" = 2 beats).

        Item Identification - TWO ways to specify which item (auto-detected):
        1. track_pos_idx (integer): the index from create_midi_item.
        2. direct_item_id (string): "MediaItem*0x..." - stable across reorders.

        Args:
            track_index: 0-based track index (REAPER's UI shows it as track 1).
            item: EITHER an integer track_pos_idx OR a string direct_item_id.
            pitch: MIDI note pitch (0-127).
            start_time: ABSOLUTE seconds (or offset from the item start with
                relative_start=True). Provide this OR start_measure.
            start_measure: ABSOLUTE "measure:beat,fraction" (or an offset with
                relative_start=True). Provide this OR start_time.
            length_time: duration in seconds; provide this OR length_measure.
            length_measure: duration as "measures:beat,fraction".
            velocity: note velocity (0-127, default 96).
            relative_start: True treats start_time/start_measure as offsets from
                the item's start; False (default) uses absolute project time.
        """
        try:
            item_props = controller.get_item_properties(track_index, item)
            if not item_props:
                return {"status": "error", "message": "MIDI item does not exist"}

            item_start = float(item_props.get("position", 0) or 0.0)
            item_length = float(item_props.get("length", 0) or 0.0)

            geometry, error = _resolve_note(
                item_start,
                item_length,
                {
                    "pitch": pitch,
                    "velocity": velocity,
                    "start_time": start_time,
                    "start_measure": start_measure,
                    "length_time": length_time,
                    "length_measure": length_measure,
                    "relative_start": relative_start,
                },
                relative_start,
                "note",
            )
            if error:
                return {"status": "error", "message": error}

            if controller.add_midi_note(track_index, item, geometry["pitch"],
                                        geometry["rel_start"], geometry["length"],
                                        geometry["velocity"]):
                return {
                    "status": "success",
                    "message": f"Added MIDI note (pitch: {geometry['pitch']}, velocity: {geometry['velocity']}) to item {item}",
                    "note": {
                        "start": {"time": geometry["abs_start"], "measure": time_to_measure(geometry["abs_start"])},
                        "length": geometry["length"],
                        "end": {"time": geometry["abs_end"], "measure": time_to_measure(geometry["abs_end"])},
                        "relative_start_mode": geometry["relative_start"],
                    },
                    "debug": {
                        "item": {
                            "start_time": item_start,
                            "start_measure": time_to_measure(item_start),
                            "length": item_length,
                            "end_time": item_start + item_length,
                            "end_measure": time_to_measure(item_start + item_length),
                        },
                        "note": {
                            "absolute": {
                                "time_pos": geometry["abs_start"],
                                "measure_pos": time_to_measure(geometry["abs_start"]),
                                "end_time": geometry["abs_end"],
                                "end_measure": time_to_measure(geometry["abs_end"]),
                            },
                            "relative": {
                                "start": geometry["rel_start"],
                                "length": geometry["length"],
                                "end": geometry["rel_end"],
                            },
                            "flags": {"relative_start_mode": geometry["relative_start"]},
                        },
                        "calculations": {
                            "time_diff": geometry["rel_start"],
                            "is_in_bounds": True,
                            "start_offset": geometry["rel_start"],
                            "end_offset": geometry["rel_end"],
                        },
                    },
                }
            return {"status": "error", "message": "Failed to add MIDI note"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to add MIDI note: {str(e)}"}
    
    @mcp.tool("add_midi_notes")
    def add_midi_notes(ctx: Context, track_index: int, item: Union[int, str], notes: List[Dict[str, Any]],
                       relative_start: bool = False) -> Dict[str, Any]:
        """Add a WHOLE part (a list of notes) to a MIDI item in ONE call.

        Prefer this tool over repeated add_midi_note calls whenever a phrase,
        bass line or drum pattern is created: every note travels in one request,
        so the number of round trips does not grow with the note count (P36
        measured 12 sequential single calls against one batch call for the same
        part). If the part already exists as a .mid file, use `insert_midi_item`
        instead. For a part that a MIDI-writing tool can generate from
        parameters - a many-note or algorithmic pattern - the file path is
        cheaper still: generate it (`mcp-midi-writer :: generate_midi`, when
        available) and insert it, instead of writing every note out here
        (measured: ~0.6 s file path for a 4-bar part, against ~0.4 s per note
        through this call).

        Validation happens BEFORE any write, for the whole list:
        - every note must start inside the item (relative start >= 0);
        - no note may end past the item end (ending exactly AT the item end is
          accepted);
        - every length must be greater than zero;
        - pitch and velocity must be integers in 0..127.
        If ANY note is invalid the ENTIRE batch is refused with a per-note
        reason and nothing is written - a mixed list never leaves partial MIDI
        content. Nothing is clipped, snapped or moved; the item is never
        resized. `status` is "success" when every note was written; a host-side
        write failure mid-batch reports "partial" with the exact notes that
        failed (validation itself can no longer produce a partial write).

        notes: list of note objects, each with
        - pitch (int, 0..127, required);
        - start_time (seconds) OR start_measure ("measure:beat,fraction");
          absolute project positions by default; with relative_start=True,
          start_time is a seconds offset from the item's start and start_measure
          is measure arithmetic from the measure CONTAINING the item start;
        - length_time (seconds) OR length_measure ("measures:beat,fraction"; a
          value resolving below 0.1 s is floored to 0.1 s by the converter);
        - velocity (int, 0..127, default 96);
        - relative_start (bool, optional per note; defaults to the call's value).

        Item Identification - TWO ways to specify which item (auto-detected):
        track_pos_idx (integer, from create_midi_item) or direct_item_id
        (string "MediaItem*0x...", stable across reorders).

        Example: 12-note hat pattern in one call (item spans measures 1-2):
            notes = [
                {"pitch": 42, "start_measure": "1:1,000", "length_measure": "0:1,500", "velocity": 90},
                {"pitch": 42, "start_measure": "1:2,000", "length_measure": "0:1,500", "velocity": 70},
                ...
                {"pitch": 42, "start_measure": "2:4,000", "length_measure": "0:1,500", "velocity": 74}
            ]

        Args:
            track_index: 0-based track index (REAPER's UI shows it as track 1).
            item: EITHER an integer track_pos_idx OR a string direct_item_id.
            notes: the note objects (see above).
            relative_start: default mode for all notes (per-note override wins).
        """
        try:
            if not isinstance(notes, list) or not notes:
                return {
                    "status": "error",
                    "message": "notes must be a non-empty list of note objects",
                    "successful_notes": [],
                    "failed_notes": [],
                }

            # Check if item exists before validating notes
            item_props = controller.get_item_properties(track_index, item)
            if not item_props:
                return {
                    "status": "error",
                    "message": "MIDI item does not exist",
                    "successful_notes": [],
                    "failed_notes": [{"error": "MIDI item does not exist"}]
                }

            item_start = float(item_props.get("position", 0) or 0.0)
            item_length = float(item_props.get("length", 0) or 0.0)

            # Phase 1: resolve + validate EVERY note before the first write.
            resolved: list[Dict[str, Any]] = []
            errors: list[Dict[str, Any]] = []
            for i, note in enumerate(notes):
                geometry, error = _resolve_note(item_start, item_length, note, relative_start, f"note[{i}]")
                if error:
                    errors.append({"index": i, "note": note, "error": error})
                else:
                    resolved.append({"index": i, "note": note, "geometry": geometry})
            if errors:
                return {
                    "status": "error",
                    "message": (
                        f"{len(errors)} of {len(notes)} notes are invalid; the whole batch was refused "
                        "and no notes were added"
                    ),
                    "successful_notes": [],
                    "failed_notes": errors,
                }

            # Phase 2: write. Validation can no longer refuse a note, so a failure
            # here is a host-side write failure and is reported per note.
            results: list[Dict[str, Any]] = []
            write_errors: list[Dict[str, Any]] = []
            for entry in resolved:
                geometry = entry["geometry"]
                try:
                    written = controller.add_midi_note(
                        track_index,
                        item,
                        geometry["pitch"],
                        geometry["rel_start"],
                        geometry["length"],
                        geometry["velocity"],
                    )
                except Exception as exc:
                    write_errors.append({"index": entry["index"], "note": entry["note"], "error": str(exc)})
                    continue
                if not written:
                    write_errors.append({
                        "index": entry["index"],
                        "note": entry["note"],
                        "error": "the host did not accept the note",
                    })
                    continue
                results.append({
                    "index": entry["index"],
                    "note": entry["note"],
                    "details": {
                        "start": {
                            "time": geometry["abs_start"],
                            "measure": time_to_measure(geometry["abs_start"]),
                        },
                        "length": geometry["length"],
                        "end": {
                            "time": geometry["abs_end"],
                            "measure": time_to_measure(geometry["abs_end"]),
                        },
                        "relative_start_mode": geometry["relative_start"],
                    },
                })

            status = "success" if not write_errors else ("partial" if results else "error")
            return {
                "status": status,
                "message": f"Added {len(results)} notes, {len(write_errors)} failed",
                "successful_notes": results,
                "failed_notes": write_errors,
            }

        except Exception as e:
            return {"status": "error", "message": f"Failed to add MIDI notes: {str(e)}"}
    
    @mcp.tool("clear_midi_item")
    def clear_midi_item(ctx: Context, track_index: int, item_id: int) -> Dict[str, Any]:
        """Clear all MIDI notes from a MIDI item."""
        try:
            if controller.clear_midi_item(track_index, item_id):
                return {"status": "success", "message": f"Cleared all notes from MIDI item {item_id}"}
            return {"status": "error", "message": "Failed to clear MIDI item"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to clear MIDI item: {str(e)}"}
            
    @mcp.tool("get_midi_notes")
    def get_midi_notes(ctx: Context, track_index: int, item_id: Union[int, str], include_invisible: bool = False) -> Dict[str, Any]:
        """Get all MIDI notes from a MIDI item.
        
        By default, returns only visible notes (within item bounds). 
        Invisible notes exist in the MIDI data but won't sound during playback.
        
        Item Identification - Accepts BOTH types:
        - track_pos_idx (integer): Simple index like 0, 1, 2
        - direct_item_id (string): Internal ID like "MediaItem*0x..."
        
        Args:
            track_index: Index of the track
            item_id: EITHER integer track_pos_idx OR string direct_item_id
            include_invisible: If True, include notes outside item bounds (won't sound). Default: False
        """
        try:
            notes = controller.get_midi_notes(track_index, item_id, include_invisible)
            return {"status": "success", "notes": notes, "count": len(notes)}
        except Exception as e:
            return {"status": "error", "message": f"Failed to get MIDI notes: {str(e)}"}

    @mcp.tool("find_midi_notes_by_pitch")
    def find_midi_notes_by_pitch(ctx: Context, pitch_min: int = 0, pitch_max: int = 127) -> Dict[str, Any]:
        """Find all MIDI notes within a specific pitch range across the project."""
        try:
            notes = controller.find_midi_notes_by_pitch(pitch_min, pitch_max)
            return {"status": "success", "notes": notes}
        except Exception as e:
            return {"status": "error", "message": f"Failed to find MIDI notes by pitch: {str(e)}"}
            
    @mcp.tool("get_selected_midi_item")
    def get_selected_midi_item(ctx: Context) -> Dict[str, Any]:
        """Get the first selected MIDI item in the project."""
        try:
            result = controller.get_selected_midi_item()
            if result:
                return {"status": "success", **result}
            else:
                return {"status": "error", "message": "No selected MIDI item found"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to get selected MIDI item: {str(e)}"}
            
    # ----- Media Item Operations -----
    
    @mcp.tool("insert_audio_item")
    def insert_audio_item(ctx: Context, track_index: int, file_path: str, 
                         start_time: Optional[float] = None, start_measure: Optional[str] = None) -> Dict[str, Any]:
        """Insert an audio file as a media item on a track.
        
        Args:
            track_index: Index of the track
            file_path: Path to the audio file
            start_time: Start position in seconds (optional if start_measure is provided)
            start_measure: Start position as "measure:beat,fraction" (optional if start_time is provided)
        """
        try:
            # Determine the time position
            if start_time is not None:
                time_pos = float(start_time)
                measure_pos = time_to_measure(time_pos)
            elif start_measure is not None:
                time_pos = position_to_time(start_measure)
                measure_pos = start_measure
            else:
                return {"status": "error", "message": "Either start_time or start_measure must be provided"}
                
            item_id = controller.insert_audio_item(track_index, file_path, time_pos)
            # Convert any type of item_id to an index
            if isinstance(item_id, str):
                # If it's a string (pointer), find its index in the track
                project = reapy.Project()
                track = project.tracks[track_index]
                for i, item in enumerate(track.items):
                    if str(item.id) == item_id:
                        return {"status": "success", "message": f"Inserted audio item at position {measure_pos} (time: {time_pos:.3f}s)", "item_id": i}
                return {"status": "error", "message": "Failed to find inserted item index"}
            elif isinstance(item_id, int):
                # If it's already an index, use it directly
                if item_id >= 0:
                    return {"status": "success", "message": f"Inserted audio item at position {measure_pos} (time: {time_pos:.3f}s)", "item_id": item_id}
            return {"status": "error", "message": "Failed to insert audio item"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to insert audio item: {str(e)}"}

    @mcp.tool("insert_midi_item")
    def insert_midi_item(ctx: Context, track_index: int, file_path: str,
                         start_time: Optional[float] = None, start_measure: Optional[str] = None) -> Dict[str, Any]:
        """Insert a MIDI file (.mid) as a media item on a track.

        This is the file-based path for a part that was created as a complete MIDI
        file elsewhere (a MIDI-writing tool): one call places the whole part. For
        an empty item filled through MCP calls instead, use `create_midi_item` +
        `add_midi_notes` (one batch call carries the whole note list). For a
        many-note or algorithmic part this is the cheapest path: a .mid written
        by `mcp-midi-writer :: generate_midi` (when available) arrives in one
        call (measured: ~0.6 s for a 4-bar part, against ~0.4 s per note through
        `add_midi_notes`), and the note list never enters the conversation.

        Args:
            track_index: Index of the track
            file_path: Path to the MIDI file
            start_time: Start position in seconds (optional if start_measure is provided)
            start_measure: Start position as "measure:beat,fraction" (optional if start_time is provided)
        """
        try:
            if start_time is not None:
                time_pos = float(start_time)
                measure_pos = time_to_measure(time_pos)
            elif start_measure is not None:
                time_pos = position_to_time(start_measure)
                measure_pos = start_measure
            else:
                return {"status": "error", "message": "Either start_time or start_measure must be provided"}

            item_id = controller.insert_midi_item(track_index, file_path, time_pos)
            if isinstance(item_id, str):
                project = reapy.Project()
                track = project.tracks[track_index]
                for i, item in enumerate(track.items):
                    if str(item.id) == item_id:
                        return {"status": "success", "message": f"Inserted MIDI item at position {measure_pos} (time: {time_pos:.3f}s)", "item_id": i}
                return {"status": "error", "message": "Failed to find inserted MIDI item index"}
            elif isinstance(item_id, int):
                if item_id >= 0:
                    return {"status": "success", "message": f"Inserted MIDI item at position {measure_pos} (time: {time_pos:.3f}s)", "item_id": item_id}
            return {"status": "error", "message": "Failed to insert MIDI item"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to insert MIDI item: {str(e)}"}
    
    @mcp.tool("duplicate_item")
    def duplicate_item(ctx: Context, track_index: int, item_id: Union[int, str], 
                      new_time: Optional[float] = None, new_measure: Optional[str] = None) -> Dict[str, Any]:
        """Duplicate an existing item on a track.
        
        Item Identification - Accepts BOTH types:
        - track_pos_idx (integer): Simple index like 0, 1, 2
        - direct_item_id (string): Internal ID like "MediaItem*0x..."
        
        Args:
            track_index: Index of the track
            item_id: EITHER integer track_pos_idx OR string direct_item_id
            new_time: New position in seconds (optional)
            new_measure: New position as "measure:beat,fraction" (optional)
        """
        try:
            # Determine the time position if any position is provided
            if new_time is not None:
                time_pos = float(new_time)
                measure_pos = time_to_measure(time_pos)
                new_position = time_pos
            elif new_measure is not None:
                time_pos = position_to_time(new_measure)
                measure_pos = new_measure
                new_position = time_pos
            else:
                new_position = None
                time_pos = None
                measure_pos = None
                
            new_item_id = controller.duplicate_item(track_index, item_id, new_position)
            # Convert any type of new_item_id to an index
            if isinstance(new_item_id, str):
                # If it's a string (pointer), find its index in the track
                project = reapy.Project()
                track = project.tracks[track_index]
                for i, item in enumerate(track.items):
                    if str(item.id) == new_item_id:
                        if new_position is not None:
                            position_msg = f" at position {measure_pos} (time: {time_pos:.3f}s)"
                        else:
                            position_msg = ""
                        return {"status": "success", "message": f"Duplicated item {item_id}{position_msg}", "item_id": i}
                return {"status": "error", "message": "Failed to find duplicated item index"}
            elif isinstance(new_item_id, int):
                # If it's already an index, use it directly
                if new_item_id >= 0:
                    if new_position is not None:
                        position_msg = f" at position {measure_pos} (time: {time_pos:.3f}s)"
                    else:
                        position_msg = ""
                    return {"status": "success", "message": f"Duplicated item {item_id}{position_msg}", "item_id": new_item_id}
            return {"status": "error", "message": "Failed to duplicate item"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to duplicate item: {str(e)}"}
    
    @mcp.tool("get_item_properties")
    def get_item_properties(ctx: Context, track_index: int, item: Union[int, str]) -> Dict[str, Any]:
        """Get properties of a media item.
        
        Item Identification - Accepts BOTH types:
        - track_pos_idx (integer): Simple index like 0, 1, 2
        - direct_item_id (string): Internal ID like "MediaItem*0x..."
        
        Args:
            track_index: Index of the track
            item_id: EITHER integer track_pos_idx OR string direct_item_id
        """
        try:
            properties = controller.get_item_properties(track_index, item)
            if properties:
                return {"status": "success", "properties": properties}
            return {"status": "error", "message": f"Failed to get properties for item {item}"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to get item properties: {str(e)}"}
    
    @mcp.tool("set_item_position")
    def set_item_position(ctx: Context, track_index: int, item_id: Union[int, str], 
                         position_time: Optional[float] = None, 
                         position_measure: Optional[str] = None) -> Dict[str, Any]:
        """Set the position of a media item.
        
        Item Identification - Accepts BOTH types:
        - track_pos_idx (integer): Simple index like 0, 1, 2
        - direct_item_id (string): Internal ID like "MediaItem*0x..."
        
        Args:
            track_index: Index of the track
            item_id: EITHER integer track_pos_idx OR string direct_item_id
            position_time: New position in seconds (optional if position_measure is provided)
            position_measure: New position as "measure:beat,fraction" (optional if position_time is provided)
                           where fraction is milliseconds (e.g., "1:1,500" = measure 1, beat 1, half beat)
        """
        try:
            # Determine the time position
            if position_time is not None:
                time_pos = float(position_time)
                measure_pos = time_to_measure(time_pos)
            elif position_measure is not None:
                time_pos = position_to_time(position_measure)
                measure_pos = position_measure
            else:
                return {"status": "error", "message": "Either position_time or position_measure must be provided"}
                
            if controller.set_item_position(track_index, item_id, time_pos):
                return {"status": "success", "message": f"Set item {item_id} position to {measure_pos} (time: {time_pos:.3f}s)"}
            return {"status": "error", "message": "Failed to set item position"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to set item position: {str(e)}"}
    
    @mcp.tool("set_item_length")
    def set_item_length(ctx: Context, track_index: int, item_id: Union[int, str],
                       length_time: Optional[float] = None,
                       length_measure: Optional[str] = None) -> Dict[str, Any]:
        """Set the length of a media item.
        
        Item Identification - Accepts BOTH types:
        - track_pos_idx (integer): Simple index like 0, 1, 2
        - direct_item_id (string): Internal ID like "MediaItem*0x..."
        
        Args:
            track_index: Index of the track
            item_id: EITHER integer track_pos_idx OR string direct_item_id
            length_time: Length in seconds from start of item (optional if length_measure is provided)
            length_measure: Length as "measure:beat,fraction" from start of item(optional if length_time is provided)
        """
        try:
            # Normalize length_measure: reject explicit zero-length "0:0,0", convert "M:0,0" -> "M+1:1,0"
            if length_measure:
                lm = length_measure.strip()
                if lm in ("0:0,0", "0:0.0"):
                    return {"status": "error", "message": "Invalid length_measure: zero length specified (0:0,0)"}
                if ":0,0" in lm or ":0.0" in lm:
                    try:
                        measure_part = int(lm.split(':')[0])
                        length_measure = f"{measure_part}:1,0"
                    except Exception:
                        return {"status": "error", "message": f"Invalid length_measure format: {length_measure}"}
            
            # Get current item position to calculate length in measures
            props = controller.get_item_properties(track_index, item_id)
            if not props:
                return {"status": "error", "message": "Failed to get item properties"}
                
            current_pos = props.get("position", 0)
            
            # Determine length
            if length_time is not None:
                length = float(length_time)
                length_pos = time_to_measure(current_pos + length)
            elif length_measure is not None:
                end_pos = position_to_time(length_measure)
                length = end_pos - current_pos
                length_pos = length_measure
            else:
                return {"status": "error", "message": "Either length_time or length_measure must be provided"}

            if controller.set_item_length(track_index, item_id, length):
                return {
                    "status": "success",
                    "message": f"Set item {item_id} length to {length_pos} (time: {length:.3f}s)"
                }
            return {"status": "error", "message": "Failed to set item length"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to set item length: {str(e)}"}
    
    @mcp.tool("delete_item")
    def delete_item(ctx: Context, track_index: int, item: Union[int, str]) -> Dict[str, Any]:
        """Delete a media item from a track.
        
        Item Identification - Accepts BOTH types:
        - track_pos_idx (integer): Simple index like 0, 1, 2
        - direct_item_id (string): Internal ID like "MediaItem*0x..."
        
        Args:
            track_index: Index of the track
            item: EITHER integer track_pos_idx OR string direct_item_id
        """
        try:
            if controller.delete_item(track_index, item):
                return {"status": "success", "message": f"Deleted item {item} from track {track_index}"}
            return {"status": "error", "message": "Failed to delete item"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to delete item: {str(e)}"}
    
    @mcp.tool("get_items_in_time_range")
    def get_items_in_time_range(ctx: Context, track_index: int, 
                               start_time: Optional[float] = None, end_time: Optional[float] = None,
                               start_measure: Optional[str] = None, end_measure: Optional[str] = None) -> Dict[str, Any]:
        """Get all items on a track within a time range.
        
        Returns items with BOTH identification methods for each item:
        
        1. track_pos_idx (integer):
           - Simple sequential index: 0, 1, 2, 3...
           - Easy to use in loops and quick operations
           - Changes if items before it are deleted
           
        2. direct_item_id (string):
           - REAPER's permanent internal ID like "MediaItem*0x..."
           - Unique stable reference that never changes
           - Use this for reliable long-term item tracking
        
        You can use either one in subsequent operations like add_midi_note.
        
        Args:
            track_index: 0-based track index (0 = first track, 1 = second, etc.)
            start_time: Start position in seconds (optional if start_measure is provided)
            end_time: End position in seconds (optional if end_measure is provided)
            start_measure: Start position as "measure:beat,fraction" (optional if start_time is provided)
            end_measure: End position as "measure:beat,fraction" (optional if end_time is provided)
            
        Returns:
            Each item includes:
            - track_pos_idx: Integer position index (0, 1, 2...)
            - direct_item_id: String internal ID ("MediaItem*...")
            - position: Item start time in seconds
            - length: Item duration in seconds
        """
        try:
            # Determine start position
            if start_time is not None:
                time_start = float(start_time)
                measure_start = time_to_measure(time_start)
            elif start_measure is not None:
                time_start = position_to_time(start_measure)
                measure_start = start_measure
            else:
                return {"status": "error", "message": "Either start_time or start_measure must be provided"}
                
            # Determine end position
            if end_time is not None:
                time_end = float(end_time)
                measure_end = time_to_measure(time_end)
            elif end_measure is not None:
                time_end = position_to_time(end_measure)
                measure_end = end_measure
            else:
                return {"status": "error", "message": "Either end_time or end_measure must be provided"}
                
            # Get items with their properties
            project = reapy.Project()
            track = project.tracks[track_index]
            items_info = []
            
            for index, item in enumerate(track.items):
                if time_start <= item.position < time_end or time_start < (item.position + item.length) <= time_end:
                    items_info.append({
                        "track_pos_idx": index,
                        "direct_item_id": str(item.id),
                        "position": item.position,
                        "length": item.length
                    })
            
            return {
                "status": "success", 
                "message": f"Found {len(items_info)} items between {measure_start} and {measure_end}",
                "items": items_info,
                "range": {
                    "start": {"time": time_start, "measure": measure_start},
                    "end": {"time": time_end, "measure": measure_end}
                }
            }
        except Exception as e:
            return {"status": "error", "message": f"Failed to get items in time range: {str(e)}"}
    
    @mcp.tool("get_selected_items")
    def get_selected_items(ctx: Context) -> Dict[str, Any]:
        """Get all selected media items in the project with their properties.
        
        Returns:
            Dict containing status and list of selected items with their properties:
            - track_index: Index of the track containing the item
            - item_index: Index of the item in its track
            - position: Start time in seconds
            - length: Length in seconds from the start of item
            - is_midi: Whether the item is a MIDI item
            - name: Item name if available
        """
        try:
            result = controller.get_selected_items()
            if result and len(result) > 0:
                return {
                    "status": "success", 
                    "message": f"Found {len(result)} selected item(s)",
                    "items": result
                }
            return {"status": "error", "message": "No selected items found"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to get selected items: {str(e)}"}
    
    @mcp.tool("get_time_signature")
    def get_time_signature(ctx: Context) -> Dict[str, Any]:
        """Get the current time signature of the project."""
        try:
            time_map = get_time_map_info()
            return {
                "status": "success",
                "time_signature": {
                    "numerator": time_map['time_sig_num'],
                    "denominator": time_map['time_sig_den'],
                    "bpm": time_map['bpm']
                }
            }
        except Exception as e:
            return {"status": "error", "message": f"Failed to get time signature: {str(e)}"}

    @mcp.tool("set_project_time_signature")
    def set_project_time_signature(ctx: Context, numerator: int, denominator: int) -> Dict[str, Any]:
        """Set the default project time signature.
        
        Args:
            numerator: Time signature numerator (e.g., 4 in 4/4)
            denominator: Time signature denominator (e.g., 4 in 4/4)
        """
        try:
            if controller.set_project_time_signature(numerator, denominator):
                return {
                    "status": "success", 
                    "message": f"Set project time signature to {numerator}/{denominator}",
                    "time_signature": {
                        "numerator": numerator,
                        "denominator": denominator
                    }
                }
            return {"status": "error", "message": "Failed to set project time signature"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to set project time signature: {str(e)}"}

    @mcp.tool("get_project_time_signature")
    def get_project_time_signature(ctx: Context) -> Dict[str, Any]:
        """Get the default project time signature."""
        try:
            time_sig = controller.get_project_time_signature()
            return {
                "status": "success",
                "time_signature": time_sig
            }
        except Exception as e:
            return {"status": "error", "message": f"Failed to get project time signature: {str(e)}"}

    @mcp.tool("set_time_signature")
    def set_time_signature(ctx: Context, numerator: int, denominator: int, 
                          position: Optional[str] = None, 
                          time: Optional[float] = None) -> Dict[str, Any]:
        """Set time signature at specified position.
        
        Args:
            numerator: Time signature numerator (e.g., 4 in 4/4)
            denominator: Time signature denominator (e.g., 4 in 4/4)
            position: Position as "measure:beat,fraction" (optional if time is provided)
                     where fraction is milliseconds (e.g., "1:1,500" = measure 1, beat 1, half beat)
            time: Position in seconds (optional if position is provided)
        """
        try:
            # Determine position
            if position is not None:
                pos = position_to_time(position)
                measure_pos = position
            elif time is not None:
                pos = float(time)
                measure_pos = time_to_measure(pos)
            else:
                pos = 0.0
                measure_pos = "1:1.000"
                
            if controller.set_time_signature(numerator, denominator, pos):
                return {
                    "status": "success", 
                    "message": f"Set time signature to {numerator}/{denominator} at position {measure_pos}",
                    "position": {"time": pos, "measure": measure_pos}
                }
            return {"status": "error", "message": "Failed to set time signature"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to set time signature: {str(e)}"}

    @mcp.tool("render_project")
    def render_project(ctx: Context, 
                      output_file: str,
                      start_time: Optional[float] = None,
                      end_time: Optional[float] = None,
                      start_measure: Optional[str] = None,
                      end_measure: Optional[str] = None,
                      samplerate: int = 44100,
                      channels: int = 2) -> Dict[str, Any]:
        """Render project to audio file.
        
        Args:
            output_file: Path to output audio file
            start_time: Start position in seconds (optional if start_measure is provided)
            end_time: End position in seconds (optional if end_measure is provided)
            start_measure: Start position as "measure:beat,fraction" (optional if start_time is provided)
            end_measure: End position as "measure:beat,fraction" (optional if end_time is provided)
            samplerate: Sample rate in Hz (default: 44100)
            channels: Number of channels (default: 2)

        NOTE (P30): this signature used to end in `**kwargs`. A `**kwargs` parameter is
        modelled by the transport as ONE **required** field called `kwargs`, so every
        real client call failed validation before the body ran - the tool was
        unreachable from the chat surface and from a raw MCP client alike (measured:
        `1 validation error for render_projectArguments / kwargs Field required`). The
        controller never used the extra keywords, so dropping them changes nothing
        about the render itself.
        """
        try:
            # Convert measure positions to time if provided
            render_start = None
            render_end = None
            
            if start_time is not None:
                render_start = float(start_time)
                start_pos = time_to_measure(render_start)
            elif start_measure is not None:
                render_start = position_to_time(start_measure)
                start_pos = start_measure
                
            if end_time is not None:
                render_end = float(end_time)
                end_pos = time_to_measure(render_end)
            elif end_measure is not None:
                render_end = position_to_time(end_measure)
                end_pos = end_measure
            
            # Perform render
            if controller.render_project(output_file, render_start, render_end, 
                                      samplerate, channels):
                response = {
                    "status": "success",
                    "message": f"Project rendered to {output_file}",
                    "file": output_file,
                    "settings": {
                        "samplerate": samplerate,
                        "channels": channels
                    }
                }
                
                # Add time range info if provided
                if render_start is not None or render_end is not None:
                    response["range"] = {
                        "start": {"time": render_start, "measure": start_pos} if render_start is not None else None,
                        "end": {"time": render_end, "measure": end_pos} if render_end is not None else None
                    }
                    
                return response
            return {"status": "error", "message": "Failed to render project"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to render project: {str(e)}"}

    @mcp.tool("save_project")
    def save_project(ctx: Context, path: str) -> Dict[str, Any]:
        """Write the CURRENT project to a .rpp file and verify it landed on disk.

        WHY THIS TOOL EXISTS: everything else in this server builds a project in memory and
        REAPER keeps it there until somebody saves it - measured on a real session, where an
        unsaved aligned project was lost to a reboot and had to be rebuilt from the bundle.
        `apply_audiocpp_manifest` can save as part of filling (its `save_as` argument), and this
        tool is the same act on its own, for any caller that has a project worth keeping.

        Args:
            path: where to write the project, ending in .rpp (parent directories are created).
                REQUIRED, and deliberately not defaulted: REAPER answers a save with no file name
                with a modal Save-As dialog, and a modal blocks every call behind it, so a
                default would be a hang rather than a convenience. The project's current file
                name cannot be read through reapy either (`GetProjectName` and `EnumProjects`
                return the buffer they are given - probed), so this tool cannot decide on its own
                whether saving in place would be safe.

        The answer reports `saved_as`, the file's `bytes`, the time it took, and `rewritten`
        (whether the mtime MOVED): a file that was already there with an unchanged mtime comes
        back with `rewritten: false` and a warning, because a caller that asked for a restore
        point needs to know the way back is older than this call.
        """
        try:
            return controller.save_project(path)
        except Exception as e:
            return {"status": "error", "message": f"Failed to save project: {str(e)}"}

    @mcp.tool("new_project_tab")
    def new_project_tab(ctx: Context, ignore_template: bool = True) -> Dict[str, Any]:
        """Open a FRESH project tab and leave it current, so a fill has an empty project to go to.

        WHY THIS IS A TOOL AND NOT A LINE IN A SCRIPT: the manifest fill writes into whatever
        project is current, and with `clean=True` it DELETES the AI_* tracks and the regions of
        the project it finds. So "align the next take" in a session that already holds an aligned
        project is a destructive operation, and the way out - a fresh tab - was the one step of
        this toolchain that lived in a script outside it.

        It is NOT destructive itself: the project that was current stays open and untouched, and
        the answer names it under `left_behind` (with `file` empty when it has never been saved,
        and a warning when it has - that project is one crash away from being lost).

        Args:
            ignore_template: open the tab WITHOUT applying REAPER's project template
                (default). A template puts tracks, tempo and routing in before anything of ours
                is written, so the fill's read-back can no longer tell its own tracks from the
                template's; the answer warns with the new tab's track count when it is not empty.

        Verified, not assumed: `Main_OnCommand` answers nothing, so the current project and the
        list of open tabs are read before AND after, and `created` is true only when one of them
        moved.
        """
        try:
            return controller.new_project_tab(ignore_template=ignore_template)
        except Exception as e:
            return {"status": "error", "message": f"Failed to open a new project tab: {str(e)}"}

    # ----- REAPER's OWN FX presets (the preset dropdown in the FX window) -----
    #
    # A different store from `plugin_presets` (the plugin's built-in browser): REAPER's set is a
    # file REAPER itself reads (measured 2026-10-18: `presets/vst3-Vital.ini`, format in
    # `utils/reaper_presets.py`), and applying one is ONE host call that lets the plugin load its
    # own state - no chunk read, no chunk write, no state-size ceiling. The host API has read,
    # select and navigate calls and NO save call (measured by enumeration), so `save_fx_preset`
    # writes REAPER's file in REAPER's format and makes the host confirm it before reporting.

    @mcp.tool("get_fx_presets")
    def get_fx_presets(ctx: Context, track_index: int, fx_index: int) -> Dict[str, Any]:
        """List REAPER's own presets for one FX slot: the current preset, the count, the file, names.

        Reads the HOST's preset channel (`TrackFX_GetPreset` / `TrackFX_GetPresetIndex` /
        `TrackFX_GetUserPresetFilename`) and the preset FILE REAPER keeps for this FX, so the two
        sources can be compared. A plugin that manages its own presets exposes none here and the
        answer says so (`channel_empty`); a file REAPER has not re-read yet shows up as
        `disagreement` because the host's numbering and the file's names then differ.

        The names are parsed from the file (no host calls per preset). Preset payloads are never
        returned - a listing is a listing.
        """
        try:
            return controller.get_fx_presets(track_index, fx_index)
        except Exception as e:
            return {"status": "error", "message": f"Failed to read FX presets: {str(e)}"}

    @mcp.tool("set_fx_preset")
    def set_fx_preset(
        ctx: Context,
        track_index: int,
        fx_index: int,
        name: Optional[str] = None,
        index: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Apply ONE of REAPER's own presets to an FX slot - by `name` OR by `index`, never both.

        This is the FAST path for a sound that already exists as a preset: the plugin loads its own
        state in one host call (`TrackFX_SetPreset` / `TrackFX_SetPresetByIndex`), with no state
        chunk read or write and therefore no state-size ceiling. The call is verified: the preset
        name and index are read before and after, and `verified` is true only when the selector
        became current - a host that answered `true` without changing the preset is reported as
        `verified: false` rather than as a success.
        """
        try:
            return controller.set_fx_preset(track_index, fx_index, name=name, index=index)
        except Exception as e:
            return {"status": "error", "message": f"Failed to set FX preset: {str(e)}"}

    @mcp.tool("navigate_fx_presets")
    def navigate_fx_presets(
        ctx: Context, track_index: int, fx_index: int, step: int = 1
    ) -> Dict[str, Any]:
        """Step through REAPER's presets for an FX slot (`TrackFX_NavigatePresets`) and report where.

        `step` may be negative to walk backwards. The answer carries `before` and `after` (name and
        index) plus `changed`, so a step that the host accepted but that did not move the preset is
        visible instead of implied.
        """
        try:
            return controller.navigate_fx_presets(track_index, fx_index, step=step)
        except Exception as e:
            return {"status": "error", "message": f"Failed to navigate FX presets: {str(e)}"}

    @mcp.tool("save_fx_preset")
    def save_fx_preset(
        ctx: Context, track_index: int, fx_index: int, name: str, dry_run: bool = True
    ) -> Dict[str, Any]:
        """Save the FX's CURRENT state as a REAPER preset (default `dry_run: true` shows the plan).

        REAPER exposes NO save call for FX presets (measured by enumerating `reapy.reascript_api`),
        so this writes REAPER's own preset file in REAPER's own measured format - the shape the
        owner's own saved presets have (`[General] NbPresets`, then `[PresetK]` with 16,385-byte
        hex chunks, `Len`, `Name`) - and then makes the HOST confirm it, because an unconfirmed
        preset is a trap: the preset count must grow, the new name must become selectable, and the
        saved bytes must drive the plugin to a fixed state of the same length. Byte-identity to
        the saved blob is NOT required - measured P43: this plugin re-serializes what it holds
        (the first application keeps the live sampler runtime values, the second reaches the
        stored ones, one PCM block stays shifted by ~4 samples); the byte difference is reported
        with its numbers in `verification.byte_difference`. Any failure restores the file,
        re-applies the preset that was current, and returns `status: error` with its numbers.

        A backup of the file is kept beside it as `<file>.reaper-mcp-backup` (written once, never
        overwritten), so the way back does not depend on this call.

        Args:
            track_index / fx_index: the FX slot whose state is saved.
            name: the preset name as it will appear in REAPER's preset dropdown.
            dry_run: when true (default) nothing is written and the plan is returned.
        """
        try:
            return controller.save_fx_preset(track_index, fx_index, name=name, dry_run=dry_run)
        except Exception as e:
            return {"status": "error", "message": f"Failed to save FX preset: {str(e)}"}

