# This file is now just a wrapper around the controllers package

import os
import sys

# Add necessary paths for imports
script_dir = os.path.dirname(os.path.abspath(__file__))
repo_root = os.path.dirname(script_dir)
sys.path.insert(0, script_dir)  # Add script directory to path

# Import controllers directly from the local directory
from controllers.base_controller import BaseController
from controllers.track_controller import TrackController
from controllers.fx_controller import FXController
from controllers.marker_controller import MarkerController
from controllers.midi_controller import MIDIController
from controllers.audio_controller import AudioController
from controllers.master_controller import MasterController
from controllers.project_controller import ProjectController
from controllers.preset_controller import PresetController

# Create a combined controller that inherits from all controllers
class ReaperController(
    TrackController,
    FXController,
    MarkerController,
    MIDIController,
    AudioController,
    MasterController,
    ProjectController,
    PresetController,
    BaseController  # BaseController must be last
):
    """Controller for interacting with Reaper using reapy.
    
    This class combines all controller functionality from specialized controllers.
    """
    pass

# Re-export the ReaperController class for backward compatibility
__all__ = ['ReaperController']
