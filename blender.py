#!/usr/bin/env python3
"""Install official Blender, report its version or run a headless recipe."""

import argparse
import sys
from pathlib import Path

from cad_tools.blender import VERSION, blender_path, blender_recipe_command, install
from cad_tools.execution import run_console
from cad_tools.workspace import prepare_workspace


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("install", help="Use installed Blender on macOS or install the pinned Linux release")
    commands.add_parser("version", help="Report the actual Blender version")
    recipe = commands.add_parser("run", help="Run a headless Blender project recipe")
    recipe.add_argument("recipe", type=Path)
    recipe.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    prepare_workspace()
    if args.command == "install":
        print(f"Blender ready: {install()}")
        return 0
    blender = blender_path()
    if args.command == "version":
        print(f"Blender {VERSION}")
    else:
        run_console(
            blender_recipe_command(blender, args.recipe.resolve(strict=True), *args.arguments),
            Path.cwd(),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
