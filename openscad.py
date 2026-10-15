#!/usr/bin/env python3
"""Install, format, lint or export OpenSCAD sources without Blender or a slicer."""

import argparse
import sys
from pathlib import Path

from cad_tools.openscad import VERSION, build_prerequisites, export_openscad, install, openscad_path
from cad_tools.openscad_source import SCA2D_IGNORE, format_sources, install_source_tools, lint_sources
from cad_tools.workspace import prepare_workspace


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("version", help="Report the actual OpenSCAD version")
    formatter = commands.add_parser("format", help="Format .scad files or source directories with SCADFormat")
    formatter.add_argument("sources", nargs="+", type=Path)
    formatter.add_argument(
        "--check", action="store_true", help="Fail on formatting differences without editing sources"
    )
    linter = commands.add_parser("lint", help="Analyze .scad files or source directories with SCA2D")
    linter.add_argument("sources", nargs="+", type=Path)
    linter.add_argument(
        "--ignore",
        default=",".join(SCA2D_IGNORE),
        help="Comma-separated message codes; replaces defaults, empty enables all",
    )
    commands.add_parser("install", help="Prepare pinned OpenSCAD, SCADFormat and SCA2D")
    commands.add_parser("build-prerequisites", help="List apt build packages needed for an uncached release")
    export = commands.add_parser("export", help="Export a model atomically to the chosen format")
    export.add_argument("source", type=Path)
    export.add_argument("output", type=Path)
    export.add_argument("-D", dest="definitions", action="append", default=[])
    args = parser.parse_args()
    prepare_workspace()
    if args.command == "format":
        return format_sources(args.sources, check=args.check)
    if args.command == "lint":
        return lint_sources(args.sources, args.ignore)
    if args.command == "install":
        executable = install()
        install_source_tools()
        print(f"OpenSCAD ready: {executable}")
        return 0
    if args.command == "build-prerequisites":
        print(" ".join(build_prerequisites()))
        return 0
    executable = openscad_path()
    if args.command == "version":
        print(f"OpenSCAD {VERSION}")
    else:
        print(
            export_openscad(executable, args.source, args.output, args.definitions),
            end="",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
