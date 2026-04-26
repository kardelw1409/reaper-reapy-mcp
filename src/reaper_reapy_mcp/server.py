from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from controllers.audio_controller import AudioController
from controllers.fx_controller import FXController
from controllers.marker_controller import MarkerController
from controllers.master_controller import MasterController
from controllers.midi_controller import MIDIController
from controllers.project_controller import ProjectController
from controllers.track_controller import TrackController
from controllers.base_controller import BaseController
from mcp_tools import setup_mcp_tools


class ReaperController(
    TrackController,
    FXController,
    MarkerController,
    MIDIController,
    AudioController,
    MasterController,
    ProjectController,
    BaseController,
):
    pass


def create_server(debug: bool = True) -> FastMCP:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    controller = ReaperController(debug=debug)
    mcp = FastMCP("Reaper Control")
    setup_mcp_tools(mcp, controller)
    return mcp


def get_selected_items_core(debug: bool = True) -> list[dict[str, Any]]:
    controller = ReaperController(debug=debug)
    return controller.get_selected_items()


def get_first_selected_audio_item_core(debug: bool = True) -> dict[str, Any] | None:
    items = get_selected_items_core(debug=debug)
    for item in items:
        if item.get("is_audio") and item.get("file_path"):
            return item
    return None


def insert_midi_files_core(
    track_index: int,
    midi_files: list[str],
    start_time: float,
    debug: bool = True,
) -> list[dict[str, Any]]:
    controller = ReaperController(debug=debug)
    inserted: list[dict[str, Any]] = []
    for midi_file in midi_files:
        item_id = controller.insert_midi_item(track_index, midi_file, start_time)
        inserted.append({"file_path": str(Path(midi_file).resolve()), "item_id": item_id})
    return inserted


def main() -> None:
    create_server(debug=True).run()
