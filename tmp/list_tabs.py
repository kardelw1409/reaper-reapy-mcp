"""List every open project tab: index, pointer, track count.

Note: EnumProjects signals the end with the STRING '(ReaProject*)0x0...0',
which is truthy - a plain `if not ptr: break` loops forever (measured).
The loop therefore stops on the zero pointer itself.
"""

from __future__ import annotations

import reapy
from reapy import reascript_api as RPR

ZERO_PTR = "(ReaProject*)0x0000000000000000"

reapy.connect()
index = 0
while True:
    row = RPR.EnumProjects(index, "", 512)
    if row[0] == ZERO_PTR or not row[0]:
        break
    project = reapy.Project(row[0])
    names = [str(t.name) for t in project.tracks]
    print(f"tab {index}: {row[0]} tracks={len(project.tracks)} first={names[:2]}")
    index += 1
print("total tabs:", index)
