"""`FileSink`: write a project to a `.rpp` file, atomically and verified.

Order is deliberate and matches `PLAN` section 6, level 1:

1. `validate(project)` - a pure check; if it returns anything, nothing is
   written and no directory is created;
2. `to_nodes` -> `render` -> UTF-8 bytes;
3. write to a temporary file in the SAME directory, then `os.replace` - a
   reader never sees half a project;
4. read the bytes back, re-parse them and compare the re-rendered text.

Step 4 is a grammar self-check, NOT proof that REAPER understands the file
(risk R7: our own parser cannot be the only witness). The live acceptance
(`PLAN` section 6, levels 3-4) is what proves that.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from ..model.project import Project
from ..rpp.parse import parse
from ..rpp.render import render_bytes
from ..validate import validate
from .to_nodes import to_nodes


class ValidationError(ValueError):
    """The model cannot be written faithfully; `problems` says why."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = tuple(problems)


class WriteVerificationError(RuntimeError):
    """The bytes on disk are not the bytes that were meant to be written."""


@dataclass(frozen=True, slots=True)
class WriteReport:
    """What was written, and what was checked."""

    path: Path
    bytes: int
    lines: int
    elapsed_ms: float
    grammar_verified: bool


class FileSink:
    """Write a `Project` to a file. No host, no reapy, no current project."""

    def write(self, project: Project, path: str | Path) -> WriteReport:
        started = time.perf_counter()
        problems = validate(project)
        if problems:
            raise ValidationError(problems)

        data = render_bytes(to_nodes(project))
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()

        written = target.read_bytes()
        if written != data:
            raise WriteVerificationError(
                "%s: bytes on disk differ from the rendered project (%d vs %d)"
                % (target, len(written), len(data))
            )
        # Grammar self-check: re-parse and re-render; a mismatch means the node
        # layer lost something, which must fail here rather than in REAPER.
        if render_bytes(parse(written.decode("utf-8"))) != data:
            raise WriteVerificationError(
                "%s: re-parsing the written file does not reproduce it" % target
            )
        return WriteReport(
            path=target,
            bytes=len(data),
            lines=data.count(b"\n"),
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            grammar_verified=True,
        )
