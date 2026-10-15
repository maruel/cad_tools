"""Streaming Studio G-code accounting. Filament slots are not physical hotends."""

import hashlib
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from cad_tools.footprints import Footprints, arc_points


@dataclass(frozen=True)
class Material:
    density_g_cm3: float
    diameter_mm: float

    def __post_init__(self) -> None:
        if any(not math.isfinite(v) or v <= 0 for v in (self.density_g_cm3, self.diameter_mm)):
            raise ValueError("Material density and filament diameter must be finite and positive")

    def mass_g(self, length_mm: float) -> float:
        return length_mm * math.pi * (self.diameter_mm / 2) ** 2 * self.density_g_cm3 / 1000


@dataclass
class GcodeAnalysis:
    version: str | None = None
    header_slots: list[int] | None = None
    header_masses_g: list[float] | None = None
    layer_heights_mm: set[float] = field(default_factory=set)
    feature_bounds_xy_mm: dict[str, list[float]] = field(default_factory=dict)
    feature_lengths_mm: dict[str, dict[int, float]] = field(default_factory=dict)
    feature_mass_g: dict[str, float] = field(default_factory=dict)
    supported_layers: set[int] = field(default_factory=set)
    # Used only to check identity of standalone and embedded toolpaths, never manifests.
    digest: str = ""
    footprints: Footprints = field(default_factory=Footprints)


def arc_bounds(
    start: tuple[float, float],
    end: tuple[float, float],
    offset: tuple[float, float],
    clockwise: bool,
) -> list[float]:
    cx, cy = start[0] + offset[0], start[1] + offset[1]
    # Studio rounds serialized endpoints. Larger radius conservatively includes them.
    radius = max(math.hypot(x - cx, y - cy) for x, y in (start, end))
    if radius == 0:
        raise ValueError("Extruded arc has zero radius")
    begin = math.atan2(start[1] - cy, start[0] - cx)
    finish = math.atan2(end[1] - cy, end[0] - cx)
    direction = -1 if clockwise else 1
    sweep = (direction * (finish - begin)) % math.tau
    if start == end:
        sweep = math.tau
    xs, ys = [start[0], end[0]], [start[1], end[1]]
    for angle in (0, math.pi / 2, math.pi, 3 * math.pi / 2):
        if (direction * (angle - begin)) % math.tau <= sweep + 1e-9:
            xs.append(cx + radius * math.cos(angle))
            ys.append(cy + radius * math.sin(angle))
    return [min(xs), max(xs), min(ys), max(ys)]


def analyze_gcode(path: Path, materials: dict[int, Material]) -> GcodeAnalysis:
    """Account positive XY deposition inside layers by feature and 1-based slot.

    Studio T0/T1/... refer to zero-based filament slots, even with H hotend
    arguments. T1000/T1001 are machine actions, not slots. Ignore machine T>=1000.
    Raw extrusion includes purge/custom routines; those lack model semantics and
    are excluded here. Header/result totals still include slicer-estimated waste.
    Relative E is required. XY absolute/relative modes and G92 are tracked; XY
    arcs require relative I/J, no R or multi-turn P. Unsupported deposition fails.
    """
    if not materials or any(not isinstance(i, int) or i <= 0 for i in materials):
        raise ValueError("Supply material facts by positive 1-based filament slot")
    result = GcodeAnalysis()
    x = y = 0.0
    absolute_xy = xy_plane = relative_centers = True
    relative_extrusion = False
    feature = ""
    layer = -1
    slot = None
    digest = hashlib.sha256()
    densities = diameters = None
    object_label = "Unlabeled model"
    line_width = None
    with path.open("rb") as stream:
        for raw in stream:
            digest.update(raw)
            line = raw.strip()
            if line.startswith(b"; BambuStudio "):
                result.version = line.split()[2].decode("ascii")
            elif line.startswith(b"; total filament weight [g] : "):
                result.header_masses_g = [float(v) for v in line.split(b":", 1)[1].split(b",")]
            elif line.startswith(b"; filament: "):
                result.header_slots = [int(v) for v in line.split(b":", 1)[1].split(b",")]
            elif line.startswith(b"; filament_density: "):
                densities = [float(v) for v in line.split(b":", 1)[1].split(b",")]
            elif line.startswith(b"; filament_diameter: "):
                diameters = [float(v) for v in line.split(b":", 1)[1].split(b",")]
            elif line.startswith(b"; printable_area = "):
                result.footprints.initialize(line.split(b"=", 1)[1].strip().decode("ascii"))
            elif line.startswith(b"; OBJECT_ID: "):
                object_label = "Object " + str(int(line.split(b":", 1)[1]))
            elif line.startswith(b"; LINE_WIDTH: "):
                line_width = float(line.split(b":", 1)[1])
            elif line.startswith(b"; CHANGE_LAYER"):
                layer += 1
            elif line.startswith(b"; FEATURE: "):
                feature = line.split(b":", 1)[1].strip().decode("ascii")
            elif line.startswith(b"; LAYER_HEIGHT: "):
                height = float(line.split(b":", 1)[1])
                if not math.isfinite(height) or height <= 0:
                    raise ValueError("Invalid layer height")
                result.layer_heights_mm.add(round(height, 3))
            code = line.split(b";", 1)[0].strip()
            if not code:
                continue
            command = code.split(maxsplit=1)[0]
            if (
                command.startswith(b"G")
                and command not in (b"G0", b"G1", b"G2", b"G3", b"G92")
                and re.search(rb"\bE[-+]?\d", code)
                and layer >= 0
                and feature not in ("", "Custom", "Flush")
            ):
                raise ValueError(f"Unsupported extrusion command: {command.decode('ascii')}")
            if command.startswith(b"T") and command[1:].isdigit():
                index = int(command[1:])
                if index < 1000:
                    slot = index + 1
                continue
            if command in (b"G90", b"G91"):
                absolute_xy = command == b"G90"
            elif command in (b"G17", b"G18", b"G19"):
                xy_plane = command == b"G17"
            elif command in (b"G90.1", b"G91.1"):
                relative_centers = command == b"G91.1"
            elif command == b"M83":
                relative_extrusion = True
            elif command == b"M82":
                raise ValueError("Toolpath accounting requires relative extrusion (M83)")
            elif command in (b"G0", b"G1", b"G2", b"G3", b"G92"):
                axes = {
                    m[1].decode("ascii"): float(m[2])
                    for m in re.finditer(rb"\b([EIJPRXY])([-+]?(?:\d*\.)?\d+)\b", code)
                }
                if any(not math.isfinite(v) for v in axes.values()):
                    raise ValueError("Nonfinite toolpath coordinate")
                old_x, old_y = x, y
                if absolute_xy or command == b"G92":
                    x, y = axes.get("X", x), axes.get("Y", y)
                else:
                    x, y = x + axes.get("X", 0), y + axes.get("Y", 0)
                arc = command in (b"G2", b"G3")
                if (
                    command == b"G92"
                    or layer < 0
                    or feature in ("", "Custom", "Flush")
                    or axes.get("E", 0) <= 0
                    or not (arc or "X" in axes or "Y" in axes)
                ):
                    continue
                if not relative_extrusion:
                    raise ValueError("Unknown extrusion mode")
                if slot not in materials:
                    raise ValueError(f"Unknown deposited filament slot: {slot}")
                bounds = [min(old_x, x), max(old_x, x), min(old_y, y), max(old_y, y)]
                if arc:
                    if (
                        not xy_plane
                        or not relative_centers
                        or "R" in axes
                        or not {"I", "J"}.intersection(axes)
                        or axes.get("P", 1) != 1
                    ):
                        raise ValueError("Unsupported extruded arc format")
                    bounds = arc_bounds(
                        (old_x, old_y),
                        (x, y),
                        (axes.get("I", 0), axes.get("J", 0)),
                        command == b"G2",
                    )
                points = (
                    arc_points((old_x, old_y), (x, y), (axes.get("I", 0), axes.get("J", 0)), command == b"G2")
                    if arc
                    else [(old_x, old_y), (x, y)]
                )
                if line_width is None:
                    raise ValueError("Missing deposited line width")
                result.footprints.deposit(
                    "Prime tower" if feature == "Prime tower" else object_label, points, line_width
                )
                limits = result.feature_bounds_xy_mm.setdefault(feature, [math.inf, -math.inf, math.inf, -math.inf])
                limits[:] = [
                    min(limits[0], bounds[0]),
                    max(limits[1], bounds[1]),
                    min(limits[2], bounds[2]),
                    max(limits[3], bounds[3]),
                ]
                lengths = result.feature_lengths_mm.setdefault(feature, {})
                lengths[slot] = lengths.get(slot, 0) + axes["E"]
                if feature in ("Support", "Support interface"):
                    result.supported_layers.add(layer)
    used_slots = {slot for lengths in result.feature_lengths_mm.values() for slot in lengths}
    if (
        densities is None
        or diameters is None
        or any(
            slot > len(densities)
            or slot > len(diameters)
            or not math.isclose(materials[slot].density_g_cm3, densities[slot - 1], abs_tol=0.001)
            or not math.isclose(materials[slot].diameter_mm, diameters[slot - 1], abs_tol=0.001)
            for slot in used_slots
        )
    ):
        raise ValueError("Supplied material facts differ from G-code density/diameter headers")
    for name, lengths in result.feature_lengths_mm.items():
        result.feature_mass_g[name] = sum(materials[i].mass_g(length) for i, length in lengths.items())
    result.digest = digest.hexdigest()
    return result


def verify_gcode(
    analysis: GcodeAnalysis,
    version: str,
    masses: dict[int, float],
    embedded: BinaryIO | None = None,
) -> None:
    """Check Studio identity, header accounting and optional archived byte identity."""
    if (
        analysis.version != version
        or analysis.header_slots is None
        or analysis.header_masses_g is None
        or len(analysis.header_slots) != len(analysis.header_masses_g)
        or set(analysis.header_slots) != set(masses)
        or any(
            not math.isfinite(mass) or mass < 0 or not math.isclose(mass, masses[slot], abs_tol=0.02)
            for slot, mass in zip(analysis.header_slots, analysis.header_masses_g, strict=True)
        )
    ):
        raise ValueError("G-code version or header masses differ from slice report")
    if embedded is not None and hashlib.file_digest(embedded, "sha256").hexdigest() != analysis.digest:
        raise ValueError("Archived G-code differs from validated toolpath")
