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
from reaper_reapy_mcp.plugins import setup_plugin_tools
from reaper_reapy_mcp.manifest import setup_manifest_tools


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
    # Universal Plugin Layer V3 (13 §3): ONE line, unconditional registration
    # (AUD-013). It adds the 7 `plugin_*` tools and touches nothing else: with no
    # provider configured every plugin tool answers a typed `UnsupportedProvider`.
    setup_plugin_tools(mcp)
    # Audiocpp manifest fill (E2): ONE line; adds apply_audiocpp_manifest and
    # touches nothing else. The bodies live in reaper_reapy_mcp/manifest/.
    setup_manifest_tools(mcp, controller)
    # Startup preload (09 §4; owner Decision 4): when a provider is configured,
    # the prepared profiles in the cache directory are loaded, validated and held
    # in memory - no host contact, no profile build, no writes, and no cost at all
    # when no provider is configured. It is bounded (see plugins/preload.py) and a
    # failure never stops the server.
    try:
        from reaper_reapy_mcp.plugins import preload as plugin_preload

        plugin_preload.preload_from_env()
    except Exception:  # noqa: BLE001 - a preload that cannot run must not stop the server
        logging.getLogger(__name__).warning("plugin profile preload did not run", exc_info=True)
    # REAPER's own error boxes freeze EVERY call - a modal blocks all reapy
    # communication (measured; T-01 in the reference troubleshooting map). The
    # dialog guard answers the boxes this project knows (render errors, render
    # incompletes, render script errors) and never touches a question about the
    # user's work. Started here so every session inherits it.
    try:
        from reaper_dialogs import start_watcher

        start_watcher()
    except Exception:  # noqa: BLE001 - a guard that cannot start must not stop the server
        logging.getLogger(__name__).warning("reaper dialog watcher did not start", exc_info=True)
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
