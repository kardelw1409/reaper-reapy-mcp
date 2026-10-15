import reapy
from reapy import reascript_api as RPR
import logging
from typing import Optional, Union
from utils.position_utils import get_time_map_info
from .base_controller import BaseController
import os
import time

class ProjectController(BaseController):
    """Controller for project-level operations in Reaper."""
    
    def set_tempo(self, bpm: float) -> bool:
        """
        Set the project tempo.
        
        Args:
            bpm (float): Tempo in beats per minute
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            project = reapy.Project()
            project.bpm = float(bpm)
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to set tempo: {e}")
            return False
    
    def get_tempo(self) -> Optional[float]:
        """
        Get the current project tempo.
        
        Returns:
            float: Current tempo in beats per minute, or None if not available
        """
        try:
            project = reapy.Project()
            return project.bpm
            
        except Exception as e:
            self.logger.error(f"Failed to get tempo: {e}")
            return None

    def get_project_time_signature(self) -> Optional[dict]:
        """Get the default project time signature."""
        try:
            time_map = get_time_map_info()
            if not time_map:
                self.logger.error("Failed to get time map info")
                return None
                
            return {
                "numerator": time_map['time_sig_num'],
                "denominator": time_map['time_sig_den'],
                "bpm": time_map['bpm']
            }
        except Exception as e:
            self.logger.error(f"Failed to get project time signature: {e}")
            return None

    def set_project_time_signature(self, numerator: int, denominator: int) -> bool:
        """Set the default project time signature at project start."""
        try:
            project = reapy.Project()
            return RPR.TimeMap_SetTimeSigAtTime(project.id, 0.0, numerator, denominator)
        except Exception as e:
            self.logger.error(f"Failed to set project time signature: {e}")
            return False

    def set_time_signature(self, numerator: int, denominator: int, position: float) -> bool:
        """Set time signature at specified position. Position can be in seconds or 'measure:beat,fraction' format."""
        try:
            project = reapy.Project()
            return RPR.TimeMap_SetTimeSigAtTime(project.id, position, numerator, denominator)
        except Exception as e:
            self.logger.error(f"Failed to set time signature: {e}")
            return False

    def render_project(self, output_file: str, 
                      start_time: Optional[float] = None,
                      end_time: Optional[float] = None,
                      samplerate: int = 44100,
                      channels: int = 2,
                      **kwargs) -> bool:
        """Render project to file - through the **proven render path**.

        Why this is not the obvious shape (measured, twice, in this workspace and
        in the reference project's history - `C:/REAPER/reaper-kv.ini` T-01):

        * ``RENDER_BOUNDSFLAG = 3`` ("time selection" bounds) together with
          ``perform_action(41824)`` makes REAPER raise a modal
          **"Render Error / Nothing to render!"** box *even when the project
          contains media*, and because a modal blocks ALL reapy communication the
          calling script then hangs forever - the failure this method used to
          reproduce on 2026-10-11.
        * The path that works (and that the reference implementation settled on)
          is ``RENDER_BOUNDSFLAG = 0`` (custom range) with explicit
          ``RENDER_STARTPOS`` / ``RENDER_ENDPOS``, fired with
          ``Main_OnCommand(42230)`` ("File: Render project, using the most recent
          render settings"), with the settings and the action issued in **one**
          in-REAPER call.

        Three guards, each answering one measured failure:

        1. **pre-flight refusal.** If no media item overlaps the requested range,
           the render is refused *before* the action is fired, because firing it
           is exactly what raises the modal. The refusal is a ``ValueError``, not
           a dialog.
        2. **stale output removal.** The wait loop below cannot tell a fresh file
           from a leftover one; a leftover once produced a false measurement.
        3. **a dialog guard while the render runs** (``reaper_dialogs``): any
           "Render Incomplete!" question is answered **No** (discard the partial
           file - answering Yes would publish a partial render as finished), and
           any "Render Error" box is acknowledged, so REAPER cannot sit blocked
           with the next reapy call behind it.

        Render settings are snapshotted and restored around the call, so the
        project's render configuration is not a side effect of this operation.
        """
        try:
            import reaper_dialogs as RD
        except Exception:  # noqa: BLE001 - the render must not fail on the guard's import
            RD = None

        try:
            project = reapy.Project()
            output_path = os.path.abspath(output_file)
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            # ---- the range, and the pre-flight that keeps the modal away --------
            items: list[tuple[float, float]] = []
            for track in project.tracks:
                for item in track.items:
                    try:
                        items.append((float(item.position), float(item.position + item.length)))
                    except Exception:  # noqa: BLE001 - an unreadable item is not media evidence
                        continue
            if not items:
                raise ValueError(
                    "nothing to render: the project has no media items, so firing the "
                    "render action would raise REAPER's modal 'Nothing to render!' box "
                    "and block every subsequent call"
                )
            if start_time is None or end_time is None:
                range_start = 0.0 if start_time is None else float(start_time)
                range_end = max(end for _start, end in items) if end_time is None else float(end_time)
            else:
                range_start, range_end = float(start_time), float(end_time)
            if range_end <= range_start:
                raise ValueError(
                    f"nothing to render: the range is empty ({range_start} .. {range_end})"
                )
            overlapping = [
                (start, end) for start, end in items if start < range_end and end > range_start
            ]
            if not overlapping:
                raise ValueError(
                    f"nothing to render: no media item overlaps {range_start:.3f}..{range_end:.3f} "
                    "(the render action was not fired, so no modal was raised)"
                )

            # ---- stale output: the wait loop must not be satisfied by a leftover --
            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except OSError as exc:  # noqa: BLE001 - reported, and the wait will fail if it matters
                    self.logger.warning(f"Could not remove the stale render output: {exc}")

            # ---- snapshot everything this method is about to set ------------------
            pid = project.id
            snap_int_keys = (
                "RENDER_BOUNDSFLAG", "RENDER_STARTPOS", "RENDER_ENDPOS",
                "RENDER_SRATE", "RENDER_CHANNELS",
            )
            snap_int = {key: float(RPR.GetSetProjectInfo(pid, key, 0.0, False)) for key in snap_int_keys}
            snap_str_keys = ("RENDER_FILE", "RENDER_PATTERN")
            snap_str = {key: str(RPR.GetSetProjectInfo_String(pid, key, "", False)[3]) for key in snap_str_keys}
            selection_before = (project.time_selection.start, project.time_selection.end)

            guard = None
            if RD is not None:
                guard = RD.start_watcher(interval_s=0.25)

            try:
                # settings AND the action in one in-REAPER call: splitting them was
                # measured to render nothing at all in the reference project
                self._configure_and_render_in_one_call(
                    range_start, range_end, int(samplerate), int(channels), output_path, kwargs
                )

                # the render is asynchronous: wait for a file that stops growing
                prev_size = -1
                for _ in range(240):  # 60 s at 0.25 s
                    if os.path.isfile(output_path):
                        size = os.path.getsize(output_path)
                        if size > 1024 and size == prev_size:
                            break
                        prev_size = size
                    time.sleep(0.25)
                    if RD is not None:
                        # answer "Render Incomplete!" / "Render Error" while blocked
                        RD.sweep_render_boxes(do=True)
            finally:
                # restore the render settings this method set; the time selection was
                # never touched (BOUNDSFLAG=0 uses explicit positions)
                try:
                    for key, value in snap_int.items():
                        RPR.GetSetProjectInfo(pid, key, value, True)
                    for key, value in snap_str.items():
                        RPR.GetSetProjectInfo_String(pid, key, value, True)
                    project.time_selection = selection_before
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning(f"Render settings restore failed: {exc}")

            if os.path.isfile(output_path):
                self.logger.debug(f"Render completed: {output_path}")
                return True

            # no file: say what the guard saw, so "it failed" is not a shrug
            seen = []
            if RD is not None:
                seen = RD.watcher_state().get("dismissed", [])
            self.logger.error(
                "Render failed - no output file appeared"
                + (f"; render dialogs handled meanwhile: {seen}" if seen else "")
            )
            return False

        except ValueError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to render project: {e}")
            return False

    @staticmethod
    @reapy.inside_reaper()
    def _configure_and_render_in_one_call(range_start: float, range_end: float,
                                          samplerate: int, channels: int,
                                          output_path: str, extra: dict) -> None:
        """Set the render range and fire the render action, inside REAPER.

        MEASURED (reference project, `vital_render.py`): separating the settings
        call from ``Main_OnCommand(42230)`` - into two in-REAPER calls, or firing
        from the client - produced **no output file at all** in three consecutive
        attempts. The shape below is the one that renders, so it is one function.
        """
        out_dir = os.path.dirname(output_path)
        out_name = os.path.basename(output_path)
        RPR.GetSetProjectInfo(0, "RENDER_BOUNDSFLAG", 0, True)   # 0 = custom range
        RPR.GetSetProjectInfo(0, "RENDER_STARTPOS", float(range_start), True)
        RPR.GetSetProjectInfo(0, "RENDER_ENDPOS", float(range_end), True)
        RPR.GetSetProjectInfo(0, "RENDER_SRATE", int(samplerate), True)
        RPR.GetSetProjectInfo(0, "RENDER_CHANNELS", int(channels), True)
        for key, value in (extra or {}).items():
            try:
                RPR.GetSetProjectInfo(0, f"RENDER_{str(key).upper()}", value, True)
            except Exception:  # noqa: BLE001 - an unknown option is not worth failing a render
                pass
        RPR.GetSetProjectInfo_String(0, "RENDER_FILE", out_dir, True)
        RPR.GetSetProjectInfo_String(0, "RENDER_PATTERN", out_name, True)
        # 42230 = "File: Render project, using the most recent render settings".
        # NOT `perform_action(41824)`: that pair (with BOUNDSFLAG=3) is the measured
        # trigger of the "Nothing to render!" modal.
        RPR.Main_OnCommand(42230, 0)

    def save_project(self, path: Optional[str] = None) -> dict:
        """Write the CURRENT project to a ``.rpp`` and verify the file landed on disk.

        A path is REQUIRED, and the refusal is the interesting part:

        * REAPER answers a plain save of a project that has no file yet with a **modal Save-As
          dialog**, and a modal blocks EVERY reapy call behind it - so the caller would HANG
          instead of getting an answer. This repo already documents that class of failure for
          the render path ("Nothing to render!"). MEASURED at the transport: the tool schema
          marks `path` required, so a call without one is refused as `Field required` BEFORE any
          of this code runs (`py -3.11` through the MCP hub, 2026-10-15); the guard below is the
          second line of defence, for direct calls on the controller.
        * The project's own file name cannot be read through reapy to decide whether a save in
          place is safe: both ``GetProjectName`` and ``EnumProjects`` return the buffer they
          were GIVEN (probed 2026-10-15: ``GetProjectName(id, "", 4096)`` came back
          ``[<proj>, "", 4096]``), so the question "does this project already have a file?"
          cannot be asked. A caller that knows the path passes it; a caller that does not gets
          a refusal naming the reason - never a dialog.

        The write is VERIFIED rather than assumed: ``Main_SaveProjectEx`` can return without
        producing anything (the manifest fill warns about exactly that), so the path is re-stat'ed
        and the answer carries the size, the time and whether the mtime MOVED. A file that was
        already there with an unchanged mtime is reported as ``rewritten: false`` rather than
        presented as a fresh save - a caller that asked for a restore point needs to know that
        the way back is older than this call.
        """
        project = reapy.Project()
        if not path:
            return {
                "status": "error",
                "message": "save_project needs a path: this project will not be saved in place",
                "hints": [
                    "pass path=... (for example <bundle_dir>/project/<name>.rpp)",
                    "saving by name raises REAPER's modal Save-As dialog when the project has no "
                    "file yet, and a modal blocks every call behind it",
                    "the current file name cannot be read through reapy, so the tool cannot tell "
                    "whether saving in place is safe",
                ],
            }
        target = os.path.abspath(os.path.expanduser(str(path)))
        stem, suffix = os.path.splitext(target)
        if not suffix:
            target = target + ".rpp"
        elif suffix.lower() != ".rpp":
            return {
                "status": "error",
                "message": "save_project writes .rpp files; got %r" % suffix,
                "hints": ["pass a path ending in .rpp"],
            }
        before_mtime = os.path.getmtime(target) if os.path.isfile(target) else None
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            started = time.perf_counter()
            RPR.Main_SaveProjectEx(project.id, target, 0)
            seconds = time.perf_counter() - started
        except Exception as e:
            self.logger.error(f"Failed to save project: {e}")
            return {"status": "error", "message": f"Failed to save project: {e}"}

        produced = next(
            (candidate for candidate in (target, target + ".rpp")
             if os.path.isfile(candidate) and os.path.getsize(candidate) > 0),
            None,
        )
        if produced is None:
            self.logger.error("Save reported no error but no file appeared at %s", target)
            return {
                "status": "error",
                "message": "Main_SaveProjectEx returned but no file appeared",
                "requested_path": target,
                "hints": [
                    "the project may be read-only, or REAPER may have refused silently",
                    "check the path and the disk before retrying",
                ],
            }
        after_mtime = os.path.getmtime(produced)
        answer = {
            "status": "success",
            "message": "Project saved to %s" % produced,
            "saved_as": produced,
            "bytes": os.path.getsize(produced),
            "seconds": round(seconds, 3),
            "rewritten": before_mtime is None or after_mtime != before_mtime,
        }
        if not answer["rewritten"]:
            answer["warning"] = (
                "the file was already there and its mtime did not move: this is not a fresh "
                "restore point"
            )
        return answer
