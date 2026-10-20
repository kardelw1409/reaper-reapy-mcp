"""audiocpp manifest tools: build a bundle into a project as TEXT, or fill a live one.

`setup_manifest_tools(mcp, controller)` registers two tools:

* `build_project_file` - bundle -> `.rpp` FILE through `rpp_builder` (F2/F3): no
  project is touched, and the host is not needed for the write itself;
* `apply_audiocpp_manifest` - the pre-existing LIVE fill (E2), unchanged.

The bodies live in apply.py (host mutations), manifest.py (pure
load/validate/clamp) and rpp_builder (pure model -> text). Registration is
additive: nothing in this package touches mcp_tools.py or the plugin layer.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict

from mcp.server.fastmcp import Context, FastMCP

from ..rpp_builder import FileSink, ValidationError, from_manifest, from_nodes, parse_file
from .apply import apply_bundle
from .manifest import ManifestError, load_manifest, resolve_bundle_dir

__all__ = ["setup_manifest_tools"]


def _safe_name(title: str) -> str:
    """A file name that survives Windows: a title is a LABEL, not a path segment."""
    cleaned = "".join(
        "-" if character in '<>:"/\\|?*' else character for character in str(title or "project")
    ).strip(" .")
    return cleaned or "project"


def setup_manifest_tools(mcp: FastMCP, controller: Any) -> None:
    """Register build_project_file and apply_audiocpp_manifest."""

    @mcp.tool("build_project_file")
    def build_project_file(
        ctx: Context,
        bundle_dir: str = "latest",
        out_path: str | None = None,
        title: str | None = None,
        open_after: bool = False,
    ) -> Dict[str, Any]:
        """Build the bundle into a `.rpp` FILE - no project is touched, no fill runs.

        WHY THIS EXISTS: the live fill inserts every note with a host call each
        (measured: 10257 trimmed notes, `items` 834.2 s of a 989.2 s fill). The
        same arrangement written as text takes well under a second, and the file
        is a document that can be diffed and kept. The live tool stays for
        "add to the project I have open".

        WHAT IS BUILT: `rpp_builder.compile` maps the manifest to the domain model
        (one item per region segment, the warp as stretch markers, tempo and meter,
        markers and regions) in the SAME order and with the SAME ids the live fill
        produces; `writer.FileSink` validates, writes atomically and re-reads the
        bytes. Measured against the live fill's own file on the dnb bundle: 14
        tracks / 57 items / 8 regions / 4405 event lines / 870 stretch records,
        positions and source offsets identical.

        Args:
            bundle_dir: the bundle, or "latest" for the exporter's pointer.
            out_path: where to write; default `<bundle>/project/<title>.rpp`.
            title: project title (default: the manifest's), used for the file name
                and for the seeded GUIDs.
            open_after: also open the written file in a NEW project tab. The tab is
                opened first and the previous project is reported as `left_behind`,
                so this never replaces what the user has open. Off by default: the
                build itself needs no host at all.

        The answer carries `path`, `bytes`, `lines`, `seconds`, the `counts` from
        the model, and the compiler's `warnings` (bundle warnings are carried, not
        swallowed). A model the grammar cannot express raises BEFORE anything is
        written, and the answer lists every reason in `problems`.
        """
        started = time.perf_counter()
        try:
            bundle = resolve_bundle_dir(bundle_dir)
            info = load_manifest(bundle)
            started_compile = time.perf_counter()
            project, compile_report = from_manifest(info, title=title)
            compile_s = time.perf_counter() - started_compile
        except ManifestError as exc:
            return {
                "status": "error",
                "ok": False,
                "message": str(exc),
                "detail": exc.detail,
                "hints": exc.hints,
            }
        except Exception as exc:  # noqa: BLE001 - a tool answer, not a traceback
            return {
                "status": "error",
                "ok": False,
                "message": "compile failed: %s: %s" % (type(exc).__name__, exc),
            }

        destination = Path(out_path) if out_path else Path(bundle) / "project" / (
            "%s.rpp" % _safe_name(project.title)
        )
        try:
            report = FileSink().write(project, destination)
        except ValidationError as exc:
            return {
                "status": "error",
                "ok": False,
                "message": "the compiled project cannot be written faithfully",
                "problems": list(exc.problems),
                "written": False,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "error",
                "ok": False,
                "message": "write failed: %s: %s" % (type(exc).__name__, exc),
            }

        # The written file is read back through the MODEL, not just re-parsed: the
        # question is whether the file says what the model meant, and that is a
        # different check from "the bytes round-trip".
        read_back = from_nodes(parse_file(report.path), title=project.title)
        counts = {
            "tracks": len(read_back.tracks),
            "items": sum(len(track.items) for track in read_back.tracks),
            "midi_items": compile_report.get("midi_items"),
            "audio_items": compile_report.get("audio_items"),
            "regions": len(read_back.regions),
            "markers": len(read_back.markers),
            "stretch_markers": sum(
                len(item.stretch_markers) for track in read_back.tracks for item in track.items
            ),
            "midi_events": sum(
                len(item.source.events)
                for track in read_back.tracks
                for item in track.items
                if hasattr(item.source, "events")
            ),
        }
        answer: Dict[str, Any] = {
            "status": "success",
            "ok": True,
            "path": str(report.path),
            "bytes": report.bytes,
            "lines": report.lines,
            "seconds": round(time.perf_counter() - started, 3),
            "seconds_compile": round(compile_s, 3),
            "seconds_write": round(report.elapsed_ms / 1000.0, 3),
            "counts": counts,
            "bpm": read_back.tempo_bpm,
            "meter": list(read_back.meter),
            "warnings": list(compile_report.get("warnings") or []),
            "project_touched": False,
        }
        if open_after:
            answer["opened"] = controller.open_project_file(str(report.path))
        return answer

    @mcp.tool("apply_audiocpp_manifest")
    def apply_audiocpp_manifest(
        ctx: Context,
        bundle_dir: str = "latest",
        dry_run: bool = False,
        clean: bool = False,
        save_as: str | None = None,
        apply_warp: bool = True,
        project_id: str | None = None,
    ) -> Dict[str, Any]:
        """Fill the CURRENT REAPER project from an audiocpp.manifest v1 bundle.

        The bundle (exported by mcp-audiocpp :: export_audiocpp_manifest) carries
        song.wav, score.abc, vocal.mid/ins.mid and manifest.json with bpm, meter,
        key, regions and (when the export measured the render's own beat grid) an
        alignment block. This tool sets tempo/meter, creates the tracks
        AI_MASTER / AI_VOCAL / AI_INSTR / MIDI_VOCAL / MIDI_INS, inserts the audio
        and the two MIDI files, creates the regions (via AddProjectMarker; the
        controller's create_region is the known save-lossy path) and verifies the
        result by re-reading the project. When the bundle also carries separated
        stems they land on AI_VOCAL/AI_INSTR (vocals/instrumental) and add
        AI_DRUMS/AI_BASS/AI_OTHER; transcribed drums/bass MIDI add MIDI_DRUMS/
        MIDI_BASS. All items sit at the manifest's audio.start_s, which is the
        arrangement origin: the export shifts everything so the render's first
        downbeat lands on bar 1. Region ends are clamped to the audio end (a
        bar-aligned edge may poke up to half a bar past it). It refuses a second
        apply unless clean=true (which first deletes the previous AI_* tracks and
        the manifest-matching regions). dry_run returns the full plan with zero
        mutations. Writes apply_report.json back into the bundle.
        bundle_dir="latest" reads the pointer file (env AUDIOCPP_MANIFEST_POINTER).
        `apply_warp=False` leaves the audio unwarped; by default, if the bundle carries a
        warp.json (mcp-audiocpp :: plan_warp), every audio item gets its stretch markers - a
        render's timing drifts by ~10 ms across a take, which no item position can follow.
        `project_id` pins WHICH project is filled (the `(ReaProject*)0x...` string
        `new_project_tab` returns): without it the fill uses the CURRENT project, and a
        session where the selection changed between the tab call and the fill then gets a
        refusal (measured live: 15 AI_* tracks from the PREVIOUS take). With it, the two
        calls act on one named project.
        """
        try:
            result = apply_bundle(
                bundle_dir,
                controller=controller,
                dry_run=dry_run,
                clean=clean,
                save_as=save_as,
                apply_warp=apply_warp,
                project_id=project_id,
            )
        except ManifestError as exc:
            return {
                "status": "error",
                "ok": False,
                "message": str(exc),
                "detail": exc.detail,
                "hints": exc.hints,
            }
        except Exception as exc:  # noqa: BLE001 - a tool answer, not a traceback
            return {
                "status": "error",
                "ok": False,
                "message": f"apply failed: {type(exc).__name__}: {exc}",
            }
        return {"status": "success", **result}
