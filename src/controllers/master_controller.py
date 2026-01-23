import reapy
import logging
from typing import Dict, Any, Optional

from .base_controller import BaseController

class MasterController(BaseController):
    """Controller for master track operations in Reaper."""
    
    def get_master_track(self) -> Dict[str, Any]:
        """Get information about the master track."""
        try:
            project = reapy.Project()
            master = project.master_track
            
            # Use send methods to get volume and pan instead of direct attributes
            # For master track, these are accessed differently in the reapy API
            volume = master.get_info_value('D_VOL')  # Get master volume
            pan = master.get_info_value('D_PAN')     # Get master pan
            
            # For mute and solo, use the appropriate API calls
            mute = bool(master.get_info_value('B_MUTE'))
            solo = bool(master.get_info_value('I_SOLO'))
            
            return {
                "volume": volume,
                "pan": pan,
                "mute": mute,
                "solo": solo
            }
        except Exception as e:
            self.logger.error(f"Failed to get master track info: {e}")
            return {}

    def set_master_volume(self, volume: float) -> bool:
        """
        Set the master track volume.
        
        Args:
            volume (float): Volume level (0.0 to 1.0)
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            project = reapy.Project()
            master = project.master_track
            master.volume = volume
            return True
        except Exception as e:
            self.logger.error(f"Failed to set master volume: {e}")
            return False

    def set_master_pan(self, pan: float) -> bool:
        """
        Set the master track pan.
        
        Args:
            pan (float): Pan value (-1.0 to 1.0)
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            project = reapy.Project()
            master = project.master_track
            master.pan = pan
            return True
        except Exception as e:
            self.logger.error(f"Failed to set master pan: {e}")
            return False

    def add_master_fx(self, fx_name: str) -> bool:
        """
        Add an FX to the master track.

        Args:
            fx_name (str): Name of the FX to add

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            project = reapy.Project()
            master = project.master_track
            fx = master.add_fx(fx_name)
            if fx:
                return True
            return False
        except Exception as e:
            self.logger.error(f"Failed to add master FX: {e}")
            return False

    def get_master_fx_list(self) -> list[Dict[str, Any]]:
        """
        Get a list of all FX on the master track.

        Returns:
            list[dict]: List of FX info dicts (index, name, enabled)
        """
        try:
            project = reapy.Project()
            master = project.master_track
            fx_list = []
            for fx_index, fx in enumerate(master.fxs):
                fx_list.append(
                    {
                        "index": fx_index,
                        "name": fx.name,
                        "enabled": fx.enabled if hasattr(fx, "enabled") else True,
                    }
                )
            return fx_list
        except Exception as e:
            self.logger.error(f"Failed to get master FX list: {e}")
            return []

    def toggle_master_mute(self, mute: Optional[bool] = None) -> bool:
        """
        Toggle or set the master track mute state.
        
        Args:
            mute (bool, optional): True to mute, False to unmute, None to toggle
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            project = reapy.Project()
            master = project.master_track
            if mute is None:
                master.mute = not master.mute
            else:
                master.mute = mute
            return True
        except Exception as e:
            self.logger.error(f"Failed to toggle master mute: {e}")
            return False

    def toggle_master_solo(self, solo: Optional[bool] = None) -> bool:
        """
        Toggle or set the master track solo state.
        
        Args:
            solo (bool, optional): True to solo, False to unsolo, None to toggle
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            project = reapy.Project()
            master = project.master_track
            if solo is None:
                master.solo = not master.solo
            else:
                master.solo = solo
            return True
        except Exception as e:
            self.logger.error(f"Failed to toggle master solo: {e}")
            return False
