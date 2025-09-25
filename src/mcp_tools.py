from mcp import types
from mcp.server.fastmcp import FastMCP, Context
from typing import Optional, Dict, Any, List, Union
import reapy
from utils.position_utils import position_to_time, time_to_measure, get_time_map_info, measure_length_to_time

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

    @mcp.tool("create_track")
    def create_track(ctx: Context, name: Optional[str] = None) -> Dict[str, Any]:
        """Create a new track in Reaper."""
        try:
            track_index = controller.create_track(name)
            return {"status": "success", "message": f"Created track {track_index}"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to create track: {str(e)}"}

    @mcp.tool("rename_track")
    def rename_track(ctx: Context, track_index: int, new_name: str) -> Dict[str, Any]:
        """Rename an existing track."""
        try:
            if controller.rename_track(track_index, new_name):
                return {"status": "success", "message": f"Renamed track {track_index} to {new_name}"}
            return {"status": "error", "message": f"Failed to rename track {track_index}"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to rename track: {str(e)}"}

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
        """Set the color of a track."""
        try:
            if controller.set_track_color(track_index, color):
                return {"status": "success", "message": f"Set color of track {track_index} to {color}"}
            return {"status": "error", "message": f"Failed to set color for track {track_index}"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to set track color: {str(e)}"}

    @mcp.tool("get_track_color")
    def get_track_color(ctx: Context, track_index: int) -> Dict[str, Any]:
        """Get the color of a track."""
        try:
            color = controller.get_track_color(track_index)
            return {"status": "success", "message": f"Color of track {track_index}: {color}"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to get track color: {str(e)}"}

    @mcp.tool("add_fx")
    def add_fx(ctx: Context, track_index: int, fx_name: str) -> Dict[str, Any]:
        """Add an FX to a track."""
        try:
            fx_index = controller.add_fx(track_index, fx_name)
            if fx_index >= 0:
                return {"status": "success", "message": f"Added FX {fx_name} to track {track_index} at index {fx_index}"}
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
        """Set an FX parameter value."""
        try:
            if controller.set_fx_param(track_index, fx_index, param_name, value):
                return {"status": "success", "message": f"Set parameter {param_name} to {value} for FX {fx_index} on track {track_index}"}
            return {"status": "error", "message": "Failed to set FX parameter"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to set FX parameter: {str(e)}"}

    @mcp.tool("get_fx_param")
    def get_fx_param(ctx: Context, track_index: int, fx_index: int, param_name: str) -> Dict[str, Any]:
        """Get an FX parameter value."""
        try:
            value = controller.get_fx_param(track_index, fx_index, param_name)
            return {"status": "success", "message": f"Parameter {param_name} value: {value}"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to get FX parameter: {str(e)}"}
            
    @mcp.tool("get_fx_param_list")
    def get_fx_param_list(ctx: Context, track_index: int, fx_index: int) -> Dict[str, Any]:
        """Get a list of all parameters for an FX."""
        try:
            param_list = controller.get_fx_param_list(track_index, fx_index)
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
            if fx_list:
                return {"status": "success", "message": f"Retrieved {len(fx_list)} FX on track {track_index}", "fx_list": fx_list}
            return {"status": "error", "message": f"Failed to get FX list for track {track_index}"}
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
            
    # ----- MIDI Operations -----
    
    @mcp.tool("create_midi_item")
    def create_midi_item(ctx: Context, track_index: int, 
                        start_time: Optional[float] = None, 
                        start_measure: Optional[str] = None,
                        length_time: Optional[float] = None,
                        length_measure: Optional[str] = None) -> Dict[str, Any]:
        """Create an empty MIDI item on a track.
        
        Args:
            track_index: Index of the track
            start_time: Start position in seconds (optional if start_measure is provided)
            start_measure: Start position as "measure:beat,fraction" (optional if start_time is provided)
                         where fraction is milliseconds (e.g., "1:1,500" = measure 1, beat 1, half beat)
            length_time: Duration in seconds from start position (from start_time or start_measure)(optional if length_measure is provided)
            length_measure: Duration as "measure:beat,fraction" from start position (e.g., "2:1,000" = 2 measures from start_time or start_measure)(optional if length_time is provided)
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
                
            # Determine length
            if length_time is not None:
                length = float(length_time)
                end_time = time_pos + length
                length_pos = time_to_measure(end_time)
            elif length_measure is not None:
                length = measure_length_to_time(length_measure, time_pos)
                end_time = time_pos + length
                length_pos = time_to_measure(end_time)
            else:
                return {"status": "error", "message": "Either length_time or length_measure must be provided"}

            item_id = controller.create_midi_item(track_index, time_pos, length)
            if (isinstance(item_id, int) and item_id >= 0) or (isinstance(item_id, str) and item_id):
                return {
                    "status": "success",
                    "message": f"Created MIDI item at position {measure_pos} with length {length_pos}",
                    "item_id": item_id,
                    "position": {"time": time_pos, "measure": measure_pos},
                    "end": {"time": end_time, "measure": length_pos}
                }
            return {"status": "error", "message": "Failed to create MIDI item"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to create MIDI item: {str(e)}"}
    
    @mcp.tool("add_midi_note")
    def add_midi_note(ctx: Context, track_index: int, item_id: int, pitch: int, 
                     start_time: Optional[float] = None, 
                     start_measure: Optional[str] = None,
                     length_time: Optional[float] = None,
                     length_measure: Optional[str] = None,
                     velocity: int = 96) -> Dict[str, Any]:
        """Add a MIDI note to a MIDI item.
        
        Args:
            track_index: Index of the track
            item_id: ID of the item
            pitch: MIDI note pitch (0-127)
            start_time: Start position in seconds (optional if start_measure is provided)
            start_measure: Start position as "measure:beat,fraction" (optional if start_time is provided)
            length_time: Duration in seconds from note start (from start_time or start_measure)(optional if length_measure is provided)
            length_measure: Duration as "measure:beat,fraction" from note start (e.g., "0:2,000" = 2 beats from start_time or start_measure)(optional if length_time is provided)
            velocity: Note velocity (0-127, default: 96)
        """
        try:
            # Get MIDI item properties for debugging
            item_props = controller.get_item_properties(track_index, item_id)
            if not item_props:
                return {"status": "error", "message": "Failed to get MIDI item properties"}
                
            item_start = item_props.get("position", 0)
            item_length = item_props.get("length", 0)
            
            # Determine the start position
            if start_time is not None:
                time_pos = float(start_time)
                measure_pos = time_to_measure(time_pos)
            elif start_measure is not None:
                time_pos = position_to_time(start_measure)
                measure_pos = start_measure
            else:
                return {"status": "error", "message": "Either start_time or start_measure must be provided"}

            # Determine the length
            if length_time is not None:
                length = max(float(length_time), 0.1)  # Minimum length 0.1s
                end_time = time_pos + length
                end_measure = time_to_measure(end_time)
            elif length_measure is not None:
                length = measure_length_to_time(length_measure, time_pos)
                end_time = time_pos + length
                end_measure = time_to_measure(end_time)
            else:
                return {"status": "error", "message": "Either length_time or length_measure must be provided"}

            relative_item_pos = (time_pos - item_start)
            # Enhanced debug info with detailed timing calculations
            debug_info = {
                "item": {
                    "start_time": item_start,
                    "start_measure": time_to_measure(item_start),
                    "length": item_length,
                    "end_time": item_start + item_length,
                    "end_measure": time_to_measure(item_start + item_length)
                },
                "note": {
                    "absolute": {
                        "time_pos": time_pos,
                        "measure_pos": measure_pos,
                        "end_time": end_time,
                        "end_measure": end_measure
                    },
                    "relative": {
                        "start": relative_item_pos,
                        "length": length,
                        "end": relative_item_pos + length
                    }
                },
                "calculations": {
                    "time_diff": time_pos - item_start,
                    "is_in_bounds": 0 <= relative_item_pos <= item_length,
                    "start_offset": relative_item_pos,
                    "end_offset": relative_item_pos + length
                }
            }

            if controller.add_midi_note(track_index, item_id, pitch, relative_item_pos, length, velocity):
                return {
                    "status": "success", 
                    "message": f"Added MIDI note (pitch: {pitch}, velocity: {velocity}) to item {item_id}",
                    "note": {
                        "start": {"time": time_pos, "measure": measure_pos},
                        "length": length,
                        "end": {"time": end_time, "measure": end_measure}
                    },
                    "debug": debug_info
                }
            return {"status": "error", "message": "Failed to add MIDI note"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to add MIDI note: {str(e)}"}
    
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
    def get_midi_notes(ctx: Context, track_index: int, item_id: int) -> Dict[str, Any]:
        """Get all MIDI notes from a MIDI item."""
        try:
            notes = controller.get_midi_notes(track_index, item_id)
            return {"status": "success", "notes": notes}
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
    
    @mcp.tool("duplicate_item")
    def duplicate_item(ctx: Context, track_index: int, item_id: int, 
                      new_time: Optional[float] = None, new_measure: Optional[str] = None) -> Dict[str, Any]:
        """Duplicate an existing item on a track.
        
        Args:
            track_index: Index of the track
            item_id: ID of the item to duplicate
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
    def get_item_properties(ctx: Context, track_index: int, item_id: int) -> Dict[str, Any]:
        """Get properties of a media item."""
        try:
            properties = controller.get_item_properties(track_index, item_id)
            if properties:
                return {"status": "success", "properties": properties}
            return {"status": "error", "message": f"Failed to get properties for item {item_id}"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to get item properties: {str(e)}"}
    
    @mcp.tool("set_item_position")
    def set_item_position(ctx: Context, track_index: int, item_id: int, 
                         position_time: Optional[float] = None, 
                         position_measure: Optional[str] = None) -> Dict[str, Any]:
        """Set the position of a media item.
        
        Args:
            track_index: Index of the track
            item_id: ID of the item
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
    def set_item_length(ctx: Context, track_index: int, item_id: int,
                       length_time: Optional[float] = None,
                       length_measure: Optional[str] = None) -> Dict[str, Any]:
        """Set the length of a media item.
        
        Args:
            track_index: Index of the track
            item_id: ID of the item
            length_time: Length in seconds from start of item (optional if length_measure is provided)
            length_measure: Length as "measure:beat,fraction" from start of item(optional if length_time is provided)
        """
        try:
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
    def delete_item(ctx: Context, track_index: int, item_id: int) -> Dict[str, Any]:
        """Delete a media item from a track."""
        try:
            if controller.delete_item(track_index, item_id):
                return {"status": "success", "message": f"Deleted item {item_id} from track {track_index}"}
            return {"status": "error", "message": "Failed to delete item"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to delete item: {str(e)}"}
    
    @mcp.tool("get_items_in_time_range")
    def get_items_in_time_range(ctx: Context, track_index: int, 
                               start_time: Optional[float] = None, end_time: Optional[float] = None,
                               start_measure: Optional[str] = None, end_measure: Optional[str] = None) -> Dict[str, Any]:
        """Get all items on a track within a time range.
        
        Args:
            track_index: Index of the track
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
                
            item_ids = controller.get_items_in_time_range(track_index, time_start, time_end)
            return {
                "status": "success", 
                "message": f"Found {len(item_ids)} items between {measure_start} and {measure_end}",
                "item_ids": item_ids,
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
                      channels: int = 2,
                      **kwargs) -> Dict[str, Any]:
        """Render project to audio file.
        
        Args:
            output_file: Path to output audio file
            start_time: Start position in seconds (optional if start_measure is provided)
            end_time: End position in seconds (optional if end_measure is provided)
            start_measure: Start position as "measure:beat,fraction" (optional if start_time is provided)
            end_measure: End position as "measure:beat,fraction" (optional if end_time is provided)
            samplerate: Sample rate in Hz (default: 44100)
            channels: Number of channels (default: 2)
            **kwargs: Additional render settings
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
                                      samplerate, channels, **kwargs):
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