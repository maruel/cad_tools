#!/usr/bin/env python3
"""Export and validate named, colored compressed STEP views from assembly-coordinate STL.

The JSON input maps local .stpz filenames to lists of AssemblyPart records:
name, path, color (#RRGGBB), and optional translation ([x,y,z] in millimeters).
Mesh paths are relative to the manifest. Geometry remains faceted. Each ZIP
contains one same-stem .stp file; the saved archive is reopened for validation.
"""

import argparse
import json
import sys
from pathlib import Path

from cad_tools.assembly import AssemblyPart
from cad_tools.step_assembly import write_views
from cad_tools.workspace import prepare_workspace


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare_workspace()
    views = json.loads(args.manifest.read_text())
    write_views(
        args.output,
        {
            filename: [
                AssemblyPart(
                    name=part["name"],
                    path=(args.manifest.parent / part["path"]).resolve(),
                    color=part["color"],
                    translation=tuple(part.get("translation", (0, 0, 0))),
                )
                for part in parts
            ]
            for filename, parts in views.items()
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
