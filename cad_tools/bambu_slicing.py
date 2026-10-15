"""Read native Studio slice results and verify actual deposited toolpaths."""

import math
import zipfile
from pathlib import Path

from cad_tools.gcode import GcodeAnalysis, Material, analyze_gcode, verify_gcode
from cad_tools.metadata import read_json

SLICED_PROJECT = "complete.sliced.3mf"


def read_slice(
    directory: Path,
    materials: dict[int, Material],
    version: str,
) -> tuple[dict[str, object], list[GcodeAnalysis]]:
    """Validate the slicer result, toolpath headers and embedded toolpath identity."""
    report = read_json(directory / "result.json")
    if report["return_code"] != 0 or report["error_string"] != "Success." or not report["sliced_plates"]:
        raise ValueError(f"Unsuccessful slice: {report.get('error_string')}")
    analyses = []
    with zipfile.ZipFile(directory / SLICED_PROJECT) as archive:
        for number, plate in enumerate(report["sliced_plates"], 1):
            if plate["warning_message"]:
                raise ValueError(f"Slicer warning on plate {number}: {plate['warning_message']}")
            masses = {int(f["id"]): float(f["total_used_g"]) for f in plate["filaments"]}
            if any(i not in materials or not math.isfinite(mass) or mass < 0 for i, mass in masses.items()) or not any(
                mass > 0 for mass in masses.values()
            ):
                raise ValueError("Missing material facts or implausible slice mass")
            analysis = analyze_gcode(directory / f"plate_{number}.gcode", materials)
            with archive.open(f"Metadata/plate_{number}.gcode") as embedded:
                verify_gcode(analysis, version, masses, embedded)
            analyses.append(analysis)
    return report, analyses
