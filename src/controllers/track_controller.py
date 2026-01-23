import reapy
from reapy import reascript_api as RPR
import logging
from typing import Optional

from .base_controller import BaseController

class TrackController(BaseController):
    """Controller for track-related operations in Reaper."""
    
    def create_track(self, name: Optional[str] = None) -> int:
        """
        Create a new track in Reaper.
        
        Args:
            name (str, optional): Name for the new track
            
        Returns:
            int: Index of the created track
        """
        try:
            project = reapy.Project()
            # Get current track count to determine where the new track will be added
            current_track_count = len(project.tracks)
            self.logger.info(f"Current track count: {current_track_count}")
            
            # Add track at the end (after all existing tracks)
            track = project.add_track(index=current_track_count, name=name or "")
            
            # Get the final track count and the actual index
            final_track_count = len(project.tracks)
            actual_index = final_track_count - 1  # Last track index
            
            self.logger.info(f"Added track. Final track count: {final_track_count}, returning index: {actual_index}")
            return actual_index

        except Exception as e:
            self.logger.error(f"Failed to create track: {e}")
            raise

    def rename_track(self, track_index: int, new_name: str) -> bool:
        """
        Rename an existing track.
        
        Args:
            track_index (int): Index of the track to rename
            new_name (str): New name for the track
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            project = reapy.Project()
            track = project.tracks[track_index]
            track.name = new_name
            return True

        except Exception as e:
            self.logger.error(f"Failed to rename track: {e}")
            return False

    def get_track_count(self) -> int:
        """Get the number of tracks in the project."""
        try:
            project = reapy.Project()
            return len(project.tracks)

        except Exception as e:
            self.logger.error(f"Failed to get track count: {e}")
            return 0
            
    def set_track_color(self, track_index: int, color: str) -> bool:
        """
        Set the color of a track.
        
        Args:
            track_index (int): Index of the track
            color (str): Hex color code (e.g., "#FF0000")
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            project = reapy.Project()
            track = project.tracks[track_index]
            # Convert hex color to RGB
            r = int(color[1:3], 16)
            g = int(color[3:5], 16)
            b = int(color[5:7], 16)
            track.color = (r, g, b)
            return True

        except Exception as e:
            self.logger.error(f"Failed to set track color: {e}")
            return False

    def get_track_color(self, track_index: int) -> str:
        """Get the color of a track."""
        try:
            project = reapy.Project()
            track = project.tracks[track_index]
            r, g, b = track.color
            # Return uppercase hex color to match expected format in tests
            return f"#{r:02X}{g:02X}{b:02X}"
            
        except Exception as e:
            self.logger.error(f"Failed to get track color: {e}")
            return "#000000"

    def move_track(self, track_index: int, new_index: int) -> bool:
        """
        Move a track to a new index (0-based).

        Args:
            track_index: Current track index
            new_index: Desired track index after move

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            project = reapy.Project()
            track_count = len(project.tracks)

            if track_index < 0 or track_index >= track_count:
                self.logger.error(f"Track index {track_index} out of range (project has {track_count} tracks)")
                return False
            if new_index < 0 or new_index >= track_count:
                self.logger.error(f"New index {new_index} out of range (project has {track_count} tracks)")
                return False
            if track_index == new_index:
                return True

            # Save current track selection
            selected_ids = []
            selected_count = RPR.CountSelectedTracks(project.id)
            for i in range(selected_count):
                track = RPR.GetSelectedTrack(project.id, i)
                if track:
                    selected_ids.append(track)

            # Clear selection
            for track in project.tracks:
                RPR.SetTrackSelected(track.id, False)

            # Select only the track to move
            track_to_move = project.tracks[track_index]
            RPR.SetTrackSelected(track_to_move.id, True)

            # Compute insertion index for ReorderSelectedTracks
            if new_index >= track_count - 1:
                before_index = -1
            elif new_index > track_index:
                before_index = new_index + 1
                if before_index >= track_count:
                    before_index = -1
            else:
                before_index = new_index

            RPR.ReorderSelectedTracks(int(before_index), 0)

            # Restore previous selection
            for track in project.tracks:
                RPR.SetTrackSelected(track.id, False)
            for track_id in selected_ids:
                try:
                    RPR.SetTrackSelected(track_id, True)
                except Exception:
                    pass

            return True
        except Exception as e:
            self.logger.error(f"Failed to move track: {e}")
            return False

    def set_track_volume(self, track_index: int, volume: float) -> bool:
        """
        Set the volume (fader) for a track.

        Args:
            track_index (int): Index of the track
            volume (float): Volume level (0.0 to 1.0, linear)

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            project = reapy.Project()
            track = project.tracks[track_index]
            try:
                track.volume = float(volume)
            except Exception:
                reapy.reascript_api.SetMediaTrackInfo_Value(track.id, "D_VOL", float(volume))
            return True
        except Exception as e:
            self.logger.error(f"Failed to set track volume: {e}")
            return False

    def get_track_volume(self, track_index: int) -> float:
        """
        Get the volume (fader) for a track.

        Args:
            track_index (int): Index of the track

        Returns:
            float: Volume level (0.0 to 1.0, linear). Returns 0.0 on failure.
        """
        try:
            project = reapy.Project()
            track = project.tracks[track_index]
            try:
                return float(track.volume)
            except Exception:
                return float(reapy.reascript_api.GetMediaTrackInfo_Value(track.id, "D_VOL"))
        except Exception as e:
            self.logger.error(f"Failed to get track volume: {e}")
            return 0.0

    def create_track_send(self, source_index: int, destination_index: int, volume: float = 1.0) -> Optional[int]:
        """
        Create a send from one track to another and set its volume.

        Args:
            source_index: Source track index
            destination_index: Destination track index
            volume: Send volume (linear, 1.0 = 0 dB)

        Returns:
            Optional[int]: Send index if successful, otherwise None
        """
        try:
            project = reapy.Project()
            track_count = len(project.tracks)

            if source_index < 0 or source_index >= track_count:
                self.logger.error(f"Source track index {source_index} out of range (project has {track_count} tracks)")
                return None
            if destination_index < 0 or destination_index >= track_count:
                self.logger.error(f"Destination track index {destination_index} out of range (project has {track_count} tracks)")
                return None

            source = project.tracks[source_index]
            dest = project.tracks[destination_index]

            send_idx = RPR.CreateTrackSend(source.id, dest.id)
            if send_idx < 0:
                self.logger.error("Failed to create track send")
                return None

            RPR.SetTrackSendInfo_Value(source.id, 0, send_idx, "D_VOL", float(volume))
            return int(send_idx)
        except Exception as e:
            self.logger.error(f"Failed to create track send: {e}")
            return None

    def set_track_folder(self, parent_index: int, first_child_index: int, last_child_index: int, compact: Optional[int] = None) -> bool:
        """
        Set a folder (subgroup) in REAPER by defining parent and child track range.

        Args:
            parent_index: Track index of the folder parent
            first_child_index: First child track index
            last_child_index: Last child track index (will be folder end)
            compact: Optional folder compact mode (0=normal, 1=small, 2=tiny)

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            project = reapy.Project()
            track_count = len(project.tracks)

            for idx in (parent_index, first_child_index, last_child_index):
                if idx < 0 or idx >= track_count:
                    self.logger.error(f"Track index {idx} out of range (project has {track_count} tracks)")
                    return False

            if not (parent_index < first_child_index <= last_child_index):
                self.logger.error("Invalid folder range: parent must be above children and range must be valid")
                return False

            parent = project.tracks[parent_index]
            reapy.reascript_api.SetMediaTrackInfo_Value(parent.id, "I_FOLDERDEPTH", 1)

            if compact is not None:
                reapy.reascript_api.SetMediaTrackInfo_Value(parent.id, "I_FOLDERCOMPACT", int(compact))

            # Set child depths: middle children 0, last child -1 to close the folder
            for idx in range(first_child_index, last_child_index + 1):
                child = project.tracks[idx]
                depth = -1 if idx == last_child_index else 0
                reapy.reascript_api.SetMediaTrackInfo_Value(child.id, "I_FOLDERDEPTH", depth)

            return True
        except Exception as e:
            self.logger.error(f"Failed to set track folder: {e}")
            return False
