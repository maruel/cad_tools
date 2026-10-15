#!/usr/bin/env python3
"""Validate or combine binary STL meshes without external CAD tools."""

import argparse
import sys
from dataclasses import asdict
from pathlib import Path

from cad_tools.reports import dumps_report
from cad_tools.stl import combine_stl, validate_stl
from cad_tools.workspace import prepare_workspace


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="Check topology, bounds and volume")
    check.add_argument("path", type=Path)
    check.add_argument("--multiple-shells", action="store_true")
    combine = commands.add_parser("combine", help="Combine validated meshes without transforms")
    combine.add_argument("output", type=Path)
    combine.add_argument("inputs", type=Path, nargs="+")
    args = parser.parse_args()
    prepare_workspace()
    if args.command == "check":
        print(
            dumps_report(asdict(validate_stl(args.path, require_one_shell=not args.multiple_shells))),
            end="",
        )
    else:
        for path in args.inputs:
            validate_stl(path, require_one_shell=False)
        combine_stl(tuple(args.inputs), args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
