import reapy
from reapy import reascript_api as RPR
from typing import Union, Tuple

def position_to_time(position: Union[float, str], project=None) -> float:
    """Convert a position to time in seconds.
    
    Args:
        position: Either a float (time in seconds) or string in format "measure:beat,fraction"
                 where measure is integer, beat is integer, and fraction is portion of beat in milliseconds
                 Example: "8:1,500" means measure 8, beat 1, half beat
        project: Optional reapy.Project instance. If None, current project is used.
        
    Returns:
        float: Time position in seconds
    """
    if isinstance(position, (int, float)):
        return float(position)
        
    if isinstance(position, str) and (':' in position or '.' in position):
        try:
            if project is None:
                project = reapy.Project()
            
            # Support both old and new formats
            if '.' in position:
                parts = position.split('.')
            else:
                parts = position.replace(',', '.').replace(':', '.').split('.')
                
            if len(parts) != 3:
                raise ValueError(f"Invalid position format: {position}. Expected format: measure:beat,fraction")
            
            measure = int(parts[0])
            beat = int(parts[1])
            beat_fraction = int(parts[2]) / 1000  # Convert milliseconds to fraction
            
            # Combine beat and beat_fraction
            full_beat = beat + beat_fraction
            
            # Convert using RPR directly
            time = RPR.TimeMap2_QNToTime(project.id, (measure - 1) * 4 + full_beat - 1)
            return time
            
        except ValueError as ve:
            raise ve
        except Exception as e:
            raise ValueError(f"Failed to convert position {position} to time: {str(e)}")
        
    return float(position)  # Try direct conversion

def time_to_measure(time: float, project=None) -> str:
    """
    Convert time in seconds to measure:beat,fraction string.
    
    Args:
        time: Time position in seconds
        project: Optional reapy.Project instance. If None, current project is used.
        
    Returns:
        str: Position as "measure:beat,fraction" string
             where measure and beat are integers, and fraction is milliseconds
    """
    if project is None:
        project = reapy.Project()
    
    try:
        # Convert using RPR directly
        qn = RPR.TimeMap2_timeToQN(project.id, time)
        
        # Calculate measure and beat (1-based)
        measure = int(qn // 4) + 1
        full_beat = (qn % 4) + 1
        
        # Split beat into integer and fraction parts
        beat = int(full_beat)
        beat_fraction = int((full_beat - beat) * 1000)  # Convert to milliseconds
        
        # Format with colon and comma separators
        return f"{measure}:{beat},{beat_fraction:03d}"
    except Exception as e:
        raise ValueError(f"Failed to convert time {time} to measure: {str(e)}")

def get_time_map_info(project=None) -> dict:
    """Get time map information for the project at start position."""
    if project is None:
        project = reapy.Project()
        
    try:
        # Get BPM and numerator from GetProjectTimeSignature2
        _, bpm, num = RPR.GetProjectTimeSignature2(project.id, 0, 0)
        
        # Time signature denominator is typically 4 in Reaper
        denom = (4 * (project.bpm / bpm))
        
        if num <= 0:
            raise ValueError("Invalid time signature values")
            
        return {
            'bpm': project.bpm,
            'time_sig_num': num,
            'time_sig_den': denom
        }
    except Exception as e:
        raise ValueError(f"Failed to get time map info: {str(e)}")

def measure_length_to_time(length_measure: str, start_time: float = 0, project=None) -> float:
    """Convert a measure length to time duration in seconds."""
    if project is None:
        project = reapy.Project()
        
    try:
        # Get current measure position
        curr_measure = time_to_measure(start_time)
        curr_parts = curr_measure.split(':')[0]  # Get just the measure number
        curr_measure_num = int(curr_parts)
        
        # Parse target length - support both : and . formats
        length_parts = length_measure.replace(':', '.').split('.')
        if len(length_parts) != 3:
            parts = length_measure.split(':')
            if len(parts) != 2:
                raise ValueError(f"Invalid measure format: {length_measure}. Expected format: measures:beat,fraction")
            measure_part = parts[0]
            beat_parts = parts[1].split(',')
            if len(beat_parts) != 2:
                raise ValueError("Invalid beat format")
            length_parts = [measure_part] + beat_parts
            
        measures = int(length_parts[0])
        beats = int(length_parts[1])
        subdivs = int(length_parts[2])
        
        # Calculate target position by adding length to current measure
        target_measure = f"{curr_measure_num + measures}:{beats},{subdivs}"
        end_time = position_to_time(target_measure, project)
        
        # Calculate duration
        duration = end_time - start_time
        return max(duration, 0.1)  # Ensure minimum note length
        
    except Exception as e:
        raise ValueError(f"Failed to convert measure length: {str(e)}")