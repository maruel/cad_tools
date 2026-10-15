#!/usr/bin/env python3
"""Bambu Studio discovery, installation, CLI invocation and printable-job operations.

Archive inspection/packaging and G-code analysis do not require an installed slicer.
"""

import argparse
import os
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

from cad_tools.bambu_job import build_job, describe_usage, read_job
from cad_tools.bambu_project import ProjectPatch, inspect_project, package_project
from cad_tools.bambu_studio import (
    install,
    studio_command,
    studio_path,
    studio_paths,
    studio_version,
)
from cad_tools.execution import run_console
from cad_tools.gcode import Material, analyze_gcode
from cad_tools.metadata import read_json
from cad_tools.reports import dumps_report
from cad_tools.stock import read_stock
from cad_tools.workspace import prepare_workspace, workspace_root


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("version", help="Report the actual Studio version; no profiles needed")
    commands.add_parser("install", help="Use installed Studio or download the latest release")
    invocation = commands.add_parser("run", help="Invoke Studio headlessly with its own CLI arguments")
    invocation.add_argument("arguments", nargs=argparse.REMAINDER)
    inspect = commands.add_parser("inspect-3mf", help="Inspect settings, parts, plates and per-instance bounds")
    inspect.add_argument("path", type=Path)
    inspect.add_argument("--full-settings", action="store_true")
    package = commands.add_parser("package-3mf", help="Patch a raw Studio archive with explicit job settings")
    package.add_argument("source", type=Path)
    package.add_argument("output", type=Path)
    package.add_argument(
        "--config",
        required=True,
        type=Path,
        help="ProjectPatch JSON; see cad_tools/bambu_project.py ProjectPatch",
    )
    analyze = commands.add_parser(
        "analyze-gcode",
        help="Analyze actual deposition, bounds and support consumption",
    )
    analyze.add_argument("path", type=Path)
    analyze.add_argument(
        "--materials",
        required=True,
        type=Path,
        help="JSON object of native filament ID to density_g_cm3/diameter_mm",
    )
    job = commands.add_parser(
        "build-job", help="Build and slice a multi-plate job from authored TOML and existing meshes"
    )
    job.add_argument("config", type=Path, help="Authored PrintJob TOML; see cad_tools/bambu_job.py")
    job.add_argument("--stock", required=True, type=Path, help="Explicit filament stock JSON")
    job.add_argument("--mesh-root", required=True, type=Path, help="Root for the TOML's relative STL paths")
    job.add_argument("--output", required=True, type=Path, help="New directory for 3MF and resolved reports")
    args = parser.parse_args()
    prepare_workspace()
    if args.command == "version":
        studio = studio_path()
        print(f"Bambu Studio {studio_version(studio)}")
    elif args.command == "install":
        launcher = install()
        print(f"Bambu Studio ready: {launcher}")
    elif args.command == "run":
        arguments = args.arguments
        if arguments[:1] == ["--"]:
            arguments = arguments[1:]
        if not arguments:
            parser.error("run requires Studio arguments after --")
        run_console(studio_command(studio_path(), *arguments), Path.cwd())
    elif args.command == "inspect-3mf":
        print(
            dumps_report(inspect_project(args.path, full_settings=args.full_settings)),
            end="",
        )
    elif args.command == "package-3mf":
        package_project(args.source, args.output, ProjectPatch.from_dict(read_json(args.config)))
    elif args.command == "analyze-gcode":
        materials = {int(slot): Material(**value) for slot, value in read_json(args.materials).items()}
        result = asdict(analyze_gcode(args.path, materials))
        result.pop("digest")
        result["layer_heights_mm"] = sorted(result["layer_heights_mm"])
        result["supported_layers"] = len(result["supported_layers"])
        print(dumps_report(result), end="")
    elif args.command == "build-job":
        job = read_job(args.config.resolve())
        stock = read_stock(args.stock)
        output = args.output.expanduser().resolve()
        if output.exists():
            raise FileExistsError(f"Use a new output directory: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        studio, profiles = studio_paths()
        staging = workspace_root() / "bambu-job"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        result = build_job(
            job,
            args.mesh_root.resolve(),
            staging / "outputs",
            staging / "validation",
            studio,
            profiles,
            dict(os.environ),
            stock=stock,
        )
        (staging / "outputs").replace(output)
        print(f"Published {output / job.filename}")
        print(describe_usage(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
