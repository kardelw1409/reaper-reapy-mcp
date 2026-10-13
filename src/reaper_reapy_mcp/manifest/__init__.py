"""audiocpp manifest fill: apply a bundle exported by mcp-audiocpp into REAPER.

`setup_manifest_tools(mcp, controller)` registers the single tool
`apply_audiocpp_manifest`; the bodies live in apply.py (host mutations) and
manifest.py (pure load/validate/clamp). Registration is additive: nothing in
this package touches mcp_tools.py or the plugin layer.
"""

from __future__ import annotations

from typing import Any, Dict

from mcp.server.fastmcp import Context, FastMCP

from .apply import apply_bundle
from .manifest import ManifestError

__all__ = ["setup_manifest_tools"]


def setup_manifest_tools(mcp: FastMCP, controller: Any) -> None:
    """Register apply_audiocpp_manifest (E2)."""

    @mcp.tool("apply_audiocpp_manifest")
    def apply_audiocpp_manifest(
        ctx: Context,
        bundle_dir: str = "latest",
        dry_run: bool = False,
        clean: bool = False,
        save_as: str | None = None,
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
        """
        try:
            result = apply_bundle(
                bundle_dir,
                controller=controller,
                dry_run=dry_run,
                clean=clean,
                save_as=save_as,
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
